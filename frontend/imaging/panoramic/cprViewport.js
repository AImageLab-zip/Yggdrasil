/**
 * The live panoramic: a curved reformat that follows the arch as it is dragged.
 *
 * ## What is live here, and what is not
 *
 * This is the **preview**. The strips that get saved and exported are baked on the CPU by
 * `seg2pano_core.js` and are byte-identical to what the Konva editor produced (decision
 * #8). The two are not the same image -- the ray-sum especially, which is a clipped
 * non-negative sum where vtk's nearest equivalent is an average -- so the surface bakes on
 * drag release and replaces this preview with the real thing. What the reader approves
 * before saving is what gets stored; what they see *while dragging* is this.
 *
 * ## Why `addActor` on a VOLUME_3D viewport
 *
 * The same reasoning `mesh/meshViewport.js:8-33` records for meshes, and it applies
 * unchanged: Cornerstone has no CPR viewport type, `addActor` is the supported way to put
 * an arbitrary vtk prop in front of its rendering engine, and doing it that way keeps the
 * context pool, the resize handling and the element lifecycle rather than standing up a
 * second render window beside them.
 *
 * Two traps carried over from that phase, both still live:
 *
 * - **`resetCamera` must not be used for framing.** `Viewport.resetCamera` multiplies the
 *   bounds radius by **10** for `VOLUME_3D`, which puts the strip on screen as a hairline.
 *   The camera is set explicitly from the strip's own extent instead.
 * - **The viewport is shared, not exclusive.** The rendering engine belongs to the volume
 *   grid; this attaches to it (F6: seven contexts, four windows, this is the fifth) rather
 *   than creating a second pool.
 */

import {
    ORIENTATION_ARRAY_NAME,
    ORIENTATION_COMPONENTS,
    archCenterline,
    viewUpSign,
} from './cprGeometry.js';

/** The viewport this surface owns on the shared engine. */
export const VIEWPORT_ID = 'ygg-panoramic-cpr';

/** The actor uid, so a rebuild replaces rather than accumulates. */
export const ACTOR_UID = 'panoramic-strip';

/** vtk's `ProjectionMode`, by the name the toolbar uses. */
export const PROJECTION_MODES = Object.freeze({
    // The baker takes a running maximum over the slab. Exactly MAX.
    mip: 0,
    // The baker takes a *clipped non-negative sum*; vtk has no such mode, and AVERAGE is
    // the same integral divided by the sample count -- so the preview differs wherever a
    // sample is negative. Named here rather than hidden, because it is the one place the
    // live view and the stored artifact are not the same function of the voxels.
    raysum: 2,
});

/**
 * Mount the strip.
 *
 * @param {object} options
 * @param {HTMLElement} options.element the container to render into.
 * @param {object} options.cornerstone injected: `{renderingEngine, coreEnums}`.
 * @param {object} options.vtk injected: `{vtkImageCPRMapper, vtkImageSlice, vtkPolyData,
 *   vtkDataArray}`.
 * @returns {object} the strip's handle.
 */
export function createCprViewport({ element, cornerstone, vtk }) {
    const { renderingEngine, coreEnums } = cornerstone;
    const { vtkImageCPRMapper, vtkImageSlice, vtkPolyData, vtkDataArray } = vtk;

    renderingEngine.enableElement({
        viewportId: VIEWPORT_ID,
        type: coreEnums.ViewportType.VOLUME_3D,
        element,
        defaultOptions: { background: [0, 0, 0] },
    });
    const viewport = renderingEngine.getViewport(VIEWPORT_ID);

    const mapper = vtkImageCPRMapper.newInstance();
    mapper.setOrientationArrayName(ORIENTATION_ARRAY_NAME);
    mapper.setUseUniformOrientation(false);
    const actor = vtkImageSlice.newInstance();
    actor.setMapper(mapper);
    let added = false;
    let framed = null;

    /**
     * Point the strip at the volume the grid loaded.
     *
     * The Cornerstone volume's own `imageData`, so the reformat samples the same voxels
     * the grid shows -- no second copy, no second upload to the GPU, and no chance of the
     * two disagreeing about the affine.
     */
    function setVolume(volume) {
        mapper.setImageData(volume.imageData);
    }

    /**
     * Rebuild the centreline from a worker geometry and redraw.
     *
     * @param {object} options
     * @param {object} options.geometry the worker's reply.
     * @param {number} options.sliceIndex
     * @param {object} options.descriptor the RAS descriptor: `{affine, dimensions, flipZ}`.
     * @param {string} options.mode `'mip'` or `'raysum'`.
     */
    function setArch({ geometry, sliceIndex, descriptor, mode }) {
        const dims = [
            descriptor.dimensions.width,
            descriptor.dimensions.height,
            descriptor.dimensions.depth,
        ];
        const built = archCenterline({
            geometry, sliceIndex, rasAffine: descriptor.affine, dims,
        });

        const centerline = vtkPolyData.newInstance();
        centerline.getPoints().setData(built.points, 3);
        centerline.getLines().setData(built.lines);
        // `addArray`, not `setTensors`: the mapper looks the orientation up by name
        // (`pointData.getArrayByName(model.orientationArrayName)`), and marking it as the
        // active tensor array is a claim about what it is *for* that nothing here needs.
        centerline.getPointData().addArray(
            vtkDataArray.newInstance({
                name: ORIENTATION_ARRAY_NAME,
                numberOfComponents: ORIENTATION_COMPONENTS,
                values: built.orientations,
            })
        );

        mapper.setCenterlineData(centerline);
        mapper.setWidth(built.width);
        mapper.setCenterPoint(built.centerPoint);
        // The slab the baker integrates over, in millimetres and samples -- taken from the
        // geometry rather than from the constants, so the two cannot drift.
        mapper.setProjectionSlabThickness(built.slabThickness);
        mapper.setProjectionSlabNumberOfSamples(built.slabSamples);
        setMode(mode);

        if (!added) {
            viewport.addActor({ uid: ACTOR_UID, actor });
            added = true;
        }
        frame(built, descriptor.flipZ);
        viewport.render();
    }

    /** Switch projection without rebuilding the centreline. */
    function setMode(mode) {
        mapper.setProjectionMode(PROJECTION_MODES[mode] ?? PROJECTION_MODES.mip);
        viewport.render?.();
    }

    /**
     * Look at the strip square on, at a scale that fits it.
     *
     * **The actor is not in patient space.** `ImageCPRMapper.computeBounds` sets
     * `[0, width, 0, height, 0, 0]`: the reformat is unrolled into a flat rectangle whose
     * *x* is the cross-section (the volume's Z, `width` millimetres of it) and whose *y*
     * is arc length along the arch. Framing it as though it sat in the volume -- which the
     * first version of this did -- puts the camera in the wrong plane entirely and shows
     * nothing.
     *
     * So the camera looks down -z at that rectangle, and `viewUp` chooses which way the
     * *cross-section* axis runs on screen. That is the axis the baked strip puts in its
     * rows, so getting it wrong makes the live preview a mirror of the artifact.
     *
     * Never `resetCamera`: it multiplies the bounds radius by **10** for `VOLUME_3D` and
     * would put the strip on screen as a hairline.
     */
    function frame(built, flipZ) {
        const bounds = actor.getBounds?.();
        if (!bounds) {
            return;
        }
        const across = bounds[1] - bounds[0];
        const along = bounds[3] - bounds[2];
        if (!(across > 0) || !(along > 0)) {
            // A centreline of zero length, which a collapsed arch can produce. Leaving the
            // previous camera is better than dividing by it.
            return;
        }
        const centre = [(bounds[0] + bounds[1]) / 2, (bounds[2] + bounds[3]) / 2, 0];
        framed = {
            focalPoint: centre,
            // Straight on. The rectangle is at z = 0, so any positive distance will do;
            // the scale is what decides how much of it is seen.
            position: [centre[0], centre[1], Math.max(across, along)],
            // `-x` so the cross-section runs down the screen the way the baked strip's
            // rows do, inverted for the volumes the baker reads back-to-front.
            viewUp: [-viewUpSign(flipZ), 0, 0],
            parallelProjection: true,
            // Half the cross-section: the strip is as tall as the volume is deep, and the
            // arch's arc length is the horizontal the viewport is free to crop.
            parallelScale: across / 2,
        };
        viewport.setCamera(framed);
        void built;
    }

    return {
        viewport,
        setVolume,
        setArch,
        setMode,
        /** For tests and for a resize: the camera this surface chose, never upstream's. */
        camera: () => framed,
        render: () => viewport.render(),
        destroy() {
            if (added) {
                viewport.removeActors?.([ACTOR_UID]);
                added = false;
            }
            renderingEngine.disableElement?.(VIEWPORT_ID);
        },
    };
}
