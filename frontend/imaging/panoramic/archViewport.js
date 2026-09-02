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
 * **The mask is an overlay, and had to become one.** The reader needs to see the mandible
 * the arch was fitted to. It was a `vtkImageSlice` in the scene, on the argument that an
 * actor keeps the mask's placement and the viewport's transform under one owner -- and it
 * was invisible, because vtk.js's forward renderer cannot compose a translucent slice with
 * a volume in either order. {@link createArchViewport}'s `setMask` has the mechanism. The
 * argument survives the move: the mask is placed by `indexToWorldLps` and projected by the
 * viewport's own `worldToCanvas`, so its position is still derived from the volume rather
 * than tracked alongside it, and {@link maskPlacement} and {@link maskCanvasTransform} are
 * pure and pinned by `frontend/tests/archSurface.test.js`.
 */

import { indexToWorldLps } from '../geometry/orientation.js';
import { ARCH_SPLINE, archSplineConfiguration } from './archSpline.js';

/** The viewport this surface owns on the shared engine. */
export const VIEWPORT_ID = 'ygg-panoramic-axial';

/** Its tool group. Separate from the grid's: these tools are not those tools. */
export const TOOL_GROUP_ID = 'ygg-panoramic';

/**
 * How close to the camera's plane counts as already being on it -- Cornerstone's own
 * `DEFAULT_EPSILON`, because it is that comparison {@link createArchViewport}'s
 * `showPlane` is staying on the correct side of.
 */
export const PLANE_EPSILON = 1e-5;

/** The mask's colour, matching the Konva editor's `rgba(91, 141, 239, 100)`. */
export const MASK_COLOR = Object.freeze([91 / 255, 141 / 255, 239 / 255]);
export const MASK_OPACITY = 100 / 255;

/**
 * Where the mask's pixel grid sits in the world: its corner and its two in-plane axes.
 *
 * The worker returns the *cleaned* mask -- closed, largest component, holes filled --
 * because that is what the polynomial was fitted through. Showing the raw labels instead
 * would show the reader a different thing from the one the arch answers to.
 *
 * **Read through the very function that places an arch control point.** `indexToWorldLps`
 * is what turns an arch index into a world position, so the mask's origin and axes are
 * derived from it too: whatever the two disagree about, they cannot disagree about this.
 * Deriving the axes rather than taking `Math.hypot` of the affine's columns is the half an
 * earlier version dropped -- a spacing is a length and has no sign, so a CBCT whose
 * in-plane axes point negative in LPS, which is the usual one, came out mirrored about the
 * origin and drawn confidently over the wrong half of the jaw.
 *
 * `axisI` and `axisJ` are the world displacement of **one** pixel step, so a caller scales
 * them by whatever it needs; their lengths are the in-plane spacing.
 *
 * @param {object} options
 * @param {object} options.descriptor the RAS descriptor.
 * @param {number} options.sliceIndex the axial slice the arch is on.
 * @returns {{origin: number[], axisI: number[], axisJ: number[]}} LPS mm.
 */
export function maskPlacement({ descriptor, sliceIndex }) {
    const { affine } = descriptor;
    const origin = indexToWorldLps(affine, [0, 0, sliceIndex]);
    const step = (i, j) =>
        indexToWorldLps(affine, [i, j, sliceIndex]).map((value, axis) => value - origin[axis]);
    return { origin, axisI: step(1, 0), axisJ: step(0, 1) };
}

/**
 * The mask as RGBA bytes, ready for `putImageData`.
 *
 * **A colour and an alpha, not a window/level.** An earlier version drew this through a
 * `colorWindow`/`colorLevel`, which is a *greyscale* ramp: the mandible came out white
 * rather than the blue the Konva editor drew, and the zero voxels came out as translucent
 * black spread over the whole slice, so the axial behind it was uniformly darkened and the
 * region meant to stand out was the only part not tinted. Background is **absent** here --
 * alpha zero -- which is what keeps the slice readable everywhere the mandible is not.
 *
 * @param {ArrayLike<number>} mask width*height, 0 or 1.
 * @param {number} length how many pixels the frame has.
 * @returns {Uint8ClampedArray} `length * 4` bytes.
 */
export function maskRgba(mask, length) {
    const bytes = new Uint8ClampedArray(length * 4);
    const [r, g, b] = MASK_COLOR.map((channel) => Math.round(channel * 255));
    const alpha = Math.round(MASK_OPACITY * 255);
    for (let index = 0; index < length; index += 1) {
        if (!mask[index]) {
            continue;
        }
        const offset = index * 4;
        bytes[offset] = r;
        bytes[offset + 1] = g;
        bytes[offset + 2] = b;
        bytes[offset + 3] = alpha;
    }
    return bytes;
}

/**
 * The canvas transform that maps mask pixel indices onto the viewport's canvas.
 *
 * An orthographic camera projects affinely, so three projected points fix the whole
 * mapping and the mask needs no per-pixel work to follow a pan, a zoom or a rotation. The
 * half-pixel shift is because a world position names a pixel's **centre** while a canvas
 * image starts at its corner.
 *
 * @param {{origin: number[], axisI: number[], axisJ: number[]}} placement
 * @param {(world: number[]) => number[]} worldToCanvas
 * @returns {number[]|null} `[a, b, c, d, e, f]` for `CanvasRenderingContext2D.transform`,
 *   or null when the projection is degenerate or did not answer with finite numbers.
 */
export function maskCanvasTransform(placement, worldToCanvas) {
    const { origin, axisI, axisJ } = placement;
    const at = (offsetI, offsetJ) =>
        worldToCanvas(
            origin.map((value, axis) => value + axisI[axis] * offsetI + axisJ[axis] * offsetJ)
        ) ?? [];
    const corner = at(-0.5, -0.5);
    const alongI = at(0.5, -0.5);
    const alongJ = at(-0.5, 0.5);
    const matrix = [
        alongI[0] - corner[0], alongI[1] - corner[1],
        alongJ[0] - corner[0], alongJ[1] - corner[1],
        corner[0], corner[1],
    ];
    if (!matrix.every(Number.isFinite)) {
        return null;
    }
    // A collapsed basis draws nothing and would leave the previous frame's mask on the
    // overlay; saying so lets the caller clear instead.
    return matrix[0] * matrix[3] - matrix[1] * matrix[2] === 0 ? null : matrix;
}

/**
 * Mount the axial editor.
 *
 * @param {object} options
 * @param {HTMLElement} options.element
 * @param {object} options.cornerstone injected: `{renderingEngine, coreEnums, toolsEnums,
 *   addTool, ToolGroupManager, annotation, tools, setVolumesForViewports}`.
 * @param {(controlPoints: number[][]) => void} [options.onArchEdited] fired on drag end.
 * @param {(controlPoints: number[][]) => void} [options.onArchDragged] fired continuously.
 */
export function createArchViewport({
    element, cornerstone, onArchEdited = () => {}, onArchDragged = () => {},
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
    /** The overlay canvas the mandible mask is drawn on -- see {@link setMask}. */
    let maskLayer = null;
    /** `{source, placement}` for the mask now on screen, or null when there is none. */
    let maskFrame = null;
    /**
     * The last world point {@link showPlane} was asked for.
     *
     * Kept because {@link reframe} has to put the camera back on the arch's plane after a
     * `resetCamera`, and the plane is not recoverable from the camera once it has moved.
     */
    let planePoint = null;
    const documentRef = element.ownerDocument;
    // `ImageData` off the element's own window rather than the global, for the same reason
    // `documentRef` is: this module is mounted against a document, and a page in a frame
    // has its own.
    const ImageDataImpl = documentRef?.defaultView?.ImageData ?? globalThis.ImageData;

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
            // The arch is open: condyle to condyle, never joined. With the default
            // `false` the tool closes the contour for the user on edit
            // (`SplineROITool.js:261,277,286-289`), which would draw a loop through the
            // tongue and hand the baker a curve it has no polynomial for.
            allowOpenSplines: true,
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
     * Move the camera onto the plane the arch is on, named by a **world point**.
     *
     * **Not by a slice index, and this is the defect that hid the whole editor.** The
     * arch, the mask and the spline are all built in the *RAS-reoriented* index space
     * `volumeSupply.rasDescriptor` produces -- `toRasVolume` permutes and flips the file
     * axes so the baker sees a canonical volume, and `descriptor.affine` is the affine
     * that permutation implies. `worker.z` counts slices in **that** array.
     *
     * Cornerstone's slice index counts something else entirely: steps along the camera's
     * own `viewPlaneNormal` across the volume's bounds, starting at the low end
     * (`getVolumeViewportScrollInfo`). For this viewport the normal is the AXIAL one,
     * `[0, 0, -1]` (`constants/mprCameraValues.js`), so its step 0 is the **superior** end
     * of the study and its index runs the opposite way from a canonical RAS `k`, which
     * runs inferior to superior. Handing one to the other is a mirror: `showSlice(k)` put
     * the camera on slice `depth - 1 - k`.
     *
     * Everything downstream then followed from the camera being on the wrong plane, which
     * is why three unrelated-looking things were reported together. The axial showed a
     * slice that was not the arch's and did not track the Z control. The mask, placed by
     * `maskPlacement` at the arch's world z, sat behind the volume and was composited
     * over. And the spline was dropped by `filterAnnotationsWithinSlice`, which keeps an
     * annotation only while `|(focalPoint - point) · viewPlaneNormal|` is under half a
     * slice -- so the arch was filed correctly, found by `getAnnotations`, and discarded
     * on the way to the screen, taking its control points and every hit test with it.
     *
     * A world point has no such ambiguity: it is the one thing the two index spaces
     * already agree about, and it is what `worldFor` -- the same function that places
     * every control point -- returns. `setViewReference` projects the focal delta onto the
     * normal and moves the camera by it (`BaseVolumeViewport.js:761-771`), which is the
     * branch `jumpToWorld` uses at `:707`. The frame of reference and the current
     * `viewPlaneNormal` are stated because that branch is gated on the first and skips a
     * re-orientation on the second.
     *
     * **A point already on the camera's plane is left alone, and that guard is not
     * defensive tidiness.** `setViewReference`'s focal-point branch projects the focal
     * delta onto the normal only when that projection is non-zero:
     * `if (!isEqual(normalDot, 0)) vec3.scale(focalDelta, useNormal, normalDot)`
     * (`BaseVolumeViewport.js:762-770`, tolerance 1e-5). When the requested point *is* on
     * the current plane -- which is exactly what a re-fit at an unchanged slice asks for --
     * the delta is never projected and the camera is translated by the whole **in-plane**
     * vector instead. The point named here is `worldFor([0, 0])`, the slice's corner, so
     * the axial jumped from the volume's centre to its corner on the first drag release
     * and again on every one after, taking the mask and the arch off-screen with it while
     * the reader was still holding the handle. Nothing about the plane had changed; there
     * was nothing to ask for.
     *
     * @param {number[]} worldPoint any point on the arch's plane, in LPS mm.
     */
    function showPlane(worldPoint) {
        planePoint = worldPoint;
        const { viewPlaneNormal, focalPoint } = viewport.getCamera();
        const offset = worldPoint.reduce(
            (total, value, axis) => total + (value - focalPoint[axis]) * viewPlaneNormal[axis],
            0
        );
        if (Math.abs(offset) <= PLANE_EPSILON) {
            return;
        }
        viewport.setViewReference({
            FrameOfReferenceUID: viewport.getFrameOfReferenceUID(),
            cameraFocalPoint: worldPoint,
            viewPlaneNormal,
        });
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
            // **`highlighted` is hover state, not a request to draw the handles, and
            // asking for it that way is why the control points were invisible.**
            // `SplineROITool.renderAnnotationInstance` draws them when
            // `activeHandleIndex !== null || newAnnotation || highlighted`, so stating
            // `highlighted: true` does put them on screen -- for exactly as long as it
            // takes the pointer to move: `AnnotationTool.mouseMoveCallback` flips the flag
            // off on the first mouse move that is not near the arch, the next render skips
            // `drawHandlesSvg`, and the drawing helper collects the untouched handle group.
            // The curve stayed, the handles went, and nothing reported anything.
            //
            // So `activeHandleIndex` is left **unstated**. `undefined !== null` is true,
            // which is the condition that draws the whole set unconditionally, and it is
            // how upstream's own `SplineROITool.hydrate` and this repo's intraoral editor
            // (`photos/stackViewport.js:addToothOutline`, signed off on a real study) both
            // put a programmatic spline's control points on screen. A drag ends by setting
            // it to `null` in `_endCallback`, which would hide them again -- harmless
            // here, because a released drag re-fits the arch and {@link setArch} rebuilds
            // the annotation from scratch.
            highlighted: false,
            invalidated: true,
            autoGenerated: false,
            // **`isVisible` is not decoration, and its default is not `true`.**
            // `filterAnnotationsWithinSlice` -- which every volume viewport's render and
            // every hit test goes through -- does a bare `if (!isVisible) continue`, so an
            // annotation that never states it is filtered out before it is drawn and
            // before the tool is ever asked whether a handle is under the pointer. That is
            // the whole of "the spline's control points are not fixable": the arch was
            // being added to the state manager correctly and then discarded on the way to
            // the screen, silently, on every frame. `isLocked` is stated for the same
            // reason -- `SplineROITool.hydrate` sets both, and this is that object built
            // by hand.
            isLocked: false,
            isVisible: true,
            metadata: {
                toolName: tools.SplineROITool.toolName,
                // The annotation store is keyed by frame of reference. `addAnnotation`
                // takes the key off the element, so the arch files correctly without
                // this -- but `filterAnnotationsWithinSlice` reads it back off the
                // metadata, and so does anything that later serialises the annotation.
                FrameOfReferenceUID: viewport.getFrameOfReferenceUID?.(),
                viewPlaneNormal: viewport.getCamera().viewPlaneNormal,
                viewUp: viewport.getCamera().viewUp,
                referencedImageId: undefined,
            },
            data: {
                handles: { points, textBox: { hasMoved: false } },
                // Open, and drawn by the arch spline -- see `archSpline.js`. The
                // `instance` is deliberately **not** built here: the tool builds it on
                // first render from `_getSplineConfig(type).Class`
                // (`SplineROITool.js:610-612`), which is `ArchSpline` because `bindTools`
                // registers it under this type. Constructing one here made a second,
                // unused spline -- `Spline`'s constructor does not read `controlPoints`,
                // so it was born empty and repopulated by `_updateSplineInstance` anyway.
                // Same rule the intraoral editor writes down at
                // `photos/stackViewport.js:312-315`.
                spline: { type: ARCH_SPLINE },
                contour: { closed: false },
                label: '',
                cachedStats: {},
            },
        };
        archUid = annotation.state.addAnnotation(drawn, element);
        viewport.render();
        return archUid;
    }

    /**
     * Put the cleaned mandible mask on the slice, replacing the previous one.
     *
     * **A 2D overlay over the canvas, not a `vtkImageSlice` in the scene.** The scene
     * route is what an axial editor reaches for first and it cannot work here, for a
     * reason that is structural rather than a matter of settings: vtk.js's forward
     * renderer runs the translucent pass **before** the volume pass
     * (`Rendering/OpenGL/ForwardPass.js`), and the volume mapper samples the depth buffer
     * every actor -- translucent ones included, since `ImageSlice.zBufferPass` delegates
     * to its opaque pass -- wrote into (`OpenGL/VolumeMapper.js:305`, `:647`). So a
     * translucent slice coplanar with the study is composited over by the study, and one
     * moved in front of it truncates the study's rays instead and blanks the slice it was
     * meant to annotate. There is no position for it that is an overlay. That is why the
     * mask was invisible while its actor was being added, positioned and coloured
     * correctly, and why nothing reported anything.
     *
     * The canvas has no such ordering: it is DOM, it sits over the viewport's own canvas,
     * and it cannot swallow a control-point drag -- `pointer-events: none`, the same rule
     * `mesh/meshViewport.js` and `screenGrid.js` state for the same reason.
     *
     * Redrawn on `IMAGE_RENDERED` rather than on a camera event, because that is the one
     * signal that covers a pan, a zoom, a slice change and a resize alike, and it is
     * emitted after the picture the mask has to line up with. Following the view costs
     * three `worldToCanvas` calls and one `drawImage` -- {@link maskCanvasTransform} --
     * not a pass over the pixels.
     *
     * @param {ArrayLike<number>|null} mask width*height, 0 or 1; null clears.
     * @param {object} descriptor the RAS descriptor.
     * @param {number} sliceIndex the axial slice the arch is on.
     */
    function setMask(mask, descriptor, sliceIndex) {
        if (!mask) {
            maskFrame = null;
            paintMask();
            return;
        }
        const { width, height } = descriptor.dimensions;
        const source = documentRef.createElement('canvas');
        source.width = width;
        source.height = height;
        source
            .getContext('2d')
            .putImageData(new ImageDataImpl(maskRgba(mask, width * height), width, height), 0, 0);
        maskFrame = { source, placement: maskPlacement({ descriptor, sliceIndex }) };
        paintMask();
    }

    /**
     * Draw the current mask onto the overlay, in the view as it now stands.
     *
     * Cheap enough to run on every render: the mask is rasterised once by {@link setMask}
     * and this only re-projects its four corners.
     */
    function paintMask() {
        const canvas = maskLayer ?? buildMaskLayer();
        if (!canvas) {
            return;
        }
        // The backing store follows the element, so the mask is not resampled twice on a
        // high-DPI display; `worldToCanvas` answers in CSS pixels, which is what the
        // scaled context speaks.
        const ratio = documentRef.defaultView?.devicePixelRatio || 1;
        const cssWidth = viewport.canvas?.clientWidth || canvas.clientWidth || 0;
        const cssHeight = viewport.canvas?.clientHeight || canvas.clientHeight || 0;
        if (canvas.width !== Math.round(cssWidth * ratio) || canvas.height !== Math.round(cssHeight * ratio)) {
            canvas.width = Math.round(cssWidth * ratio);
            canvas.height = Math.round(cssHeight * ratio);
        }
        const context = canvas.getContext('2d');
        context.setTransform(ratio, 0, 0, ratio, 0, 0);
        context.clearRect(0, 0, cssWidth, cssHeight);
        if (!maskFrame) {
            return;
        }
        const matrix = maskCanvasTransform(maskFrame.placement, (world) =>
            viewport.worldToCanvas(world)
        );
        if (!matrix) {
            return;
        }
        context.transform(...matrix);
        // Nearest, not linear: a mask is a membership, and interpolating it draws a blue
        // fringe half a pixel wide around every boundary the worker did not put there.
        context.imageSmoothingEnabled = false;
        context.drawImage(maskFrame.source, 0, 0);
    }

    function buildMaskLayer() {
        if (!documentRef?.createElement) {
            return null;
        }
        // Inside Cornerstone's own `.viewport-element`, which it creates `position:
        // relative` (`helpers/getOrCreateCanvas.js:22`). Appending to the host element
        // instead would place the overlay against whatever ancestor the page happens to
        // have positioned.
        const host = element.querySelector?.('.viewport-element') ?? element;
        maskLayer = documentRef.createElement('canvas');
        Object.assign(maskLayer.style, {
            position: 'absolute',
            top: '0',
            left: '0',
            width: '100%',
            height: '100%',
            pointerEvents: 'none',
        });
        // **Before the annotation layer, not after it.** Cornerstone's tools create their
        // `.svg-layer` at `enableElement` time and neither it nor the mask sets a
        // `z-index`, so paint order is DOM order -- appending would put the mandible over
        // the arch and its control points, which are the things being edited.
        host.insertBefore(maskLayer, host.querySelector?.(':scope > .svg-layer') ?? null);
        element.addEventListener(coreEnums.Events.IMAGE_RENDERED, paintMask);
        return maskLayer;
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

    /**
     * Re-fit the view to the element it is actually on screen at.
     *
     * The editor's stage is `hidden` for as long as the patient page has not been asked
     * for it, so this viewport is routinely enabled against an element that cannot be
     * measured -- `getOrCreateCanvas` then leaves the canvas at the HTML default 300x150
     * and `sWidth`/`sHeight` follow it. Everything downstream is scaled to that canvas:
     * the copy that puts the slice on screen, and `worldToCanvas`, which is what places
     * the mask overlay *and* every control point the tools draw. That is the whole of
     * "the axial, the spline and the handles moved into the top-left corner together" --
     * three layers agreeing with each other and with a canvas a third the size of the
     * box it is displayed in.
     *
     * `renderingEngine.resize` is the documented cure and is the caller's to invoke,
     * because the engine is shared with the volume grid and one call covers every
     * viewport on it. What is left here is the half only this module can do: a camera
     * fitted to a 300x150 viewport is not a camera, so the first sizing that has a real
     * element throws it away and re-establishes the arch's plane -- `resetCamera` centres
     * the focal point on the volume, and {@link showPlane} is what puts it back on the
     * slice the arch was fitted to.
     *
     * @param {boolean} [reset] whether to refit the camera as well as redraw.
     */
    function reframe(reset = false) {
        if (reset) {
            viewport.resetCamera?.({ resetPan: true, resetZoom: true, resetToCenter: true });
            if (planePoint) {
                showPlane(planePoint);
            }
        }
        // The mask follows on `IMAGE_RENDERED`; the render is what asks for both.
        viewport.render();
    }

    return {
        viewport,
        setVolume,
        setArch,
        setMask,
        showPlane,
        reframe,
        readArch,
        bindEditing,
        toolGroup: () => toolGroup,
        destroy() {
            if (archUid) {
                annotation.state.removeAnnotation(archUid);
                archUid = null;
            }
            // The same function that was registered, or the listener outlives the
            // viewport and the next render paints against a destroyed engine.
            element.removeEventListener?.(coreEnums.Events.IMAGE_RENDERED, paintMask);
            maskLayer?.remove?.();
            maskLayer = null;
            maskFrame = null;
            ToolGroupManager.destroyToolGroup?.(TOOL_GROUP_ID);
            renderingEngine.disableElement?.(VIEWPORT_ID);
        },
    };
}
