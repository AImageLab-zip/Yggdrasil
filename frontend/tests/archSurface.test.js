/**
 * The axial arch editor's three halves that only a browser used to exercise: the
 * annotation Cornerstone is asked to draw, the plane the camera is moved to, and the
 * mandible mask's placement and colouring.
 *
 * All three are pinned against the *library's own* code rather than against a description
 * of it. `filterAnnotationsWithinSlice` is the function every volume viewport's render and
 * every hit test passes through, so running the real one over the real annotation is the
 * only check that would have caught an arch that was filed correctly and then silently
 * dropped on the way to the screen -- and it is also what makes the plane test meaningful,
 * because putting the camera one slice away is how the arch, the mask and the axial were
 * all lost at once.
 */

import test from 'node:test';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

import { utilities as toolsUtilities } from '@cornerstonejs/tools';

import {
    MASK_COLOR,
    MASK_OPACITY,
    createArchViewport,
    maskCanvasTransform,
    maskPlacement,
    maskRgba,
} from '../imaging/panoramic/archViewport.js';
import { ARCH_SPLINE } from '../imaging/panoramic/archSpline.js';
import { indexToWorldLps } from '../imaging/geometry/orientation.js';

const CAMERA = Object.freeze({ viewPlaneNormal: [0, 0, 1], viewUp: [0, -1, 0], focalPoint: [0, 0, 5] });
const FRAME_OF_REFERENCE = '1.2.840.10008.1.4';

/** A `createArchViewport` whose Cornerstone is entirely fake but shaped like the real one. */
function archSurface() {
    const added = [];
    const references = [];
    const toolOptions = [];
    const resets = [];
    const viewport = {
        resetCamera: (options) => resets.push(options),
        getCamera: () => ({ ...CAMERA }),
        getFrameOfReferenceUID: () => FRAME_OF_REFERENCE,
        setViewReference: (reference) => references.push(reference),
        render() {},
        // The overlay is DOM, and this fake has none: `setMask` degrades to a no-op rather
        // than throwing, which is what keeps the annotation tests above independent of it.
        canvas: null,
        worldToCanvas: (world) => [world[0], world[1]],
    };
    const cornerstone = {
        renderingEngine: { id: 'engine', enableElement() {}, getViewport: () => viewport },
        coreEnums: {
            ViewportType: { ORTHOGRAPHIC: 'orthographic' },
            OrientationAxis: { AXIAL: 'axial' },
            Events: { IMAGE_RENDERED: 'CORNERSTONE_IMAGE_RENDERED' },
        },
        toolsEnums: { MouseBindings: {}, Events: {} },
        addTool() {},
        ToolGroupManager: {
            createToolGroup: () => ({
                addViewport() {},
                addTool: (name, options) => toolOptions.push({ name, options }),
                setToolActive() {},
            }),
        },
        annotation: {
            state: {
                addAnnotation: (drawn) => {
                    added.push(drawn);
                    return `uid-${added.length}`;
                },
                removeAnnotation() {},
                getAnnotation: () => added[added.length - 1],
            },
        },
        tools: { PanTool: { toolName: 'Pan' }, ZoomTool: { toolName: 'Zoom' }, SplineROITool: { toolName: 'SplineROI' } },
        setVolumesForViewports: async () => {},
    };
    const element = { ownerDocument: null, addEventListener() {}, removeEventListener() {} };
    return { added, references, resets, toolOptions, arch: createArchViewport({ element, cornerstone }) };
}

test('the drawn arch survives the filter every render and every hit test goes through', () => {
    // The defect this pins: an annotation that omits `isVisible` is filed by the state
    // manager, returned by `getAnnotations`, and then dropped by `filterAnnotationsWithinSlice`
    // -- so the spline is not drawn, no handle is ever found under the pointer, and the
    // control points cannot be moved. Nothing reports it.
    const { added, arch } = archSurface();
    arch.setArch([[10, 20], [30, 40], [50, 60]], ([x, y]) => [x, y, 5]);

    assert.equal(added.length, 1);
    const kept = toolsUtilities.planar.filterAnnotationsWithinSlice(added, { ...CAMERA }, 1);
    assert.deepEqual(kept, added, 'the arch must reach the screen');

    // And the fact that makes the assertion above worth making: the same arch without
    // `isVisible` is discarded by the same call, with no warning anywhere.
    const unstated = [{ ...added[0], isVisible: undefined }];
    assert.deepEqual(
        toolsUtilities.planar.filterAnnotationsWithinSlice(unstated, { ...CAMERA }, 1),
        [],
        'an annotation that does not state isVisible is never drawn'
    );
});

test('the arch carries the identity fields SplineROITool.hydrate sets', () => {
    const { added, arch } = archSurface();
    arch.setArch([[1, 1], [2, 2], [3, 3]], ([x, y]) => [x, y, 5]);
    const drawn = added[0];

    assert.equal(drawn.isVisible, true);
    assert.equal(drawn.isLocked, false);
    assert.equal(drawn.metadata.FrameOfReferenceUID, FRAME_OF_REFERENCE);
    assert.equal(drawn.data.contour.closed, false, 'an arch is open: closing it loops through the tongue');
});

test('the arch names its spline type and lets the tool build the instance', () => {
    // The defect this pins: `instance: new ArchSpline(...)`. `SplineROITool` rebuilds the
    // instance on first render only when it is falsy (`SplineROITool.js:610-612`), so a
    // hand-built one is the one that gets used -- and a hand-built one that is subtly
    // wrong (here: a base class with no `getTransformMatrix`) throws inside
    // `renderAnnotationInstance` before a single handle is drawn. Leaving it unset is the
    // rule `photos/stackViewport.js:312-315` writes down.
    const { added, arch } = archSurface();
    arch.setArch([[1, 1], [2, 2], [3, 3]], ([x, y]) => [x, y, 5]);

    assert.equal(added[0].data.spline.type, ARCH_SPLINE);
    assert.equal(added[0].data.spline.instance, undefined);
});

test('the spline tool is told the arch is open', () => {
    // `allowOpenSplines` is a *tool* configuration key. The spline config carried an
    // invented `allowOpen` triple instead, which upstream merges in and never reads, so
    // the tool ran on its `false` default and closed the contour on edit
    // (`SplineROITool.js:286-289`).
    const { toolOptions, arch } = archSurface();
    return arch.setVolume('volume-id').then(() => {
        const spline = toolOptions.find((entry) => entry.name === 'SplineROI');
        assert.ok(spline, 'the spline tool was added to the group');
        assert.equal(spline.options.allowOpenSplines, true);
        assert.equal(spline.options.spline.type, ARCH_SPLINE);
    });
});

test('the arch leaves activeHandleIndex unstated, which is what draws its control points', () => {
    // The reported defect: the control points were not visible. `SplineROITool` draws them
    // when `activeHandleIndex !== null || newAnnotation || highlighted`, and the arch used
    // to ask for `highlighted: true` with `activeHandleIndex: null` -- which draws them
    // once and then never again, because `highlighted` is *hover* state that
    // `AnnotationTool.mouseMoveCallback` flips off on the first mouse move away from the
    // curve. Unstated is how upstream's own `hydrate` does it, and `undefined !== null`.
    const { added, arch } = archSurface();
    arch.setArch([[1, 1], [2, 2], [3, 3]], ([x, y]) => [x, y, 5]);
    const drawn = added[0];

    assert.equal('activeHandleIndex' in drawn.data.handles, false);
    assert.notEqual(drawn.data.handles.activeHandleIndex, null, 'null is the one value that hides them');

    // Both upstream halves, pinned so a 5.9 that changes either fails the build here.
    const spline = readFileSync(
        'node_modules/@cornerstonejs/tools/dist/esm/tools/annotation/SplineROITool.js',
        'utf8'
    );
    assert.match(
        spline,
        /!annotationLocked && !this\.editData && activeHandleIndex !== null/,
        'The handle-drawing condition moved; re-derive what makes an arch show its control points.'
    );
    assert.match(
        readFileSync(
            'node_modules/@cornerstonejs/tools/dist/esm/tools/base/AnnotationTool.js',
            'utf8'
        ),
        /notNearToolAndMarkedActive[\s\S]{0,120}annotation\.highlighted = !annotation\.highlighted/,
        'highlighted is no longer hover state; the comment in archViewport.setArch needs revisiting.'
    );
});

/** A descriptor whose in-plane axes point *negative* in LPS, which is the usual CBCT. */
function descriptor() {
    return {
        dimensions: { width: 3, height: 2, depth: 8 },
        // RAS affine with a positive diagonal: x and y negate on the way to LPS, so an
        // identity direction would mirror the mask about the origin.
        affine: [
            [0.4, 0, 0, -20],
            [0, 0.5, 0, -30],
            [0, 0, 0.6, -40],
            [0, 0, 0, 1],
        ],
    };
}

test('the camera is moved to the arch plane by a world point, never by a slice index', () => {
    // The defect this pins, and the reason three unrelated-looking things were reported
    // together. The arch's `z` counts slices of the *RAS-reoriented* array
    // `volumeSupply.rasDescriptor` builds; Cornerstone's slice index counts steps along
    // its camera normal from the low end of the volume, and for AXIAL that normal is
    // [0, 0, -1] -- so its index runs the opposite way from a canonical RAS k, and
    // `showSlice(k)` landed on `depth - 1 - k`. The axial then showed a slice that was not
    // the arch's, the mask sat behind the volume, and the spline was thrown away by
    // `filterAnnotationsWithinSlice` for being off-plane.
    const { arch, references } = archSurface();
    const plane = indexToWorldLps(descriptor().affine, [0, 0, 3]);
    arch.showPlane(plane);

    assert.equal(references.length, 1);
    const [reference] = references;
    assert.deepEqual(reference.cameraFocalPoint, plane, 'the plane is named in world mm');
    assert.equal(reference.sliceIndex, undefined, 'a slice index would be a foreign index');
    // The two fields `BaseVolumeViewport.setViewReference` gates the focal-point branch
    // on: a mismatched frame of reference reaches the final `else` and *throws*, and a
    // normal that is neither equal nor negated re-orients the viewport instead of moving
    // it.
    assert.equal(reference.FrameOfReferenceUID, FRAME_OF_REFERENCE);
    assert.deepEqual(reference.viewPlaneNormal, CAMERA.viewPlaneNormal);
});

test('a plane the camera is already on is not asked for, because asking pans the axial', () => {
    // The reported defect: dragging any control point made the axial slice jump away
    // under the reader's hand. A released drag re-fits at the *same* slice, `onGeometry`
    // calls `showPlane(worldFor([0, 0]))` again, and `setViewReference` projects the focal
    // delta onto the normal only when that projection is non-zero -- so for a point that
    // is already on the plane it translates the camera by the entire *in-plane* delta
    // instead, which for the slice's corner is most of the volume.
    const { references, arch } = archSurface();
    const onPlane = [CAMERA.focalPoint[0] + 40, CAMERA.focalPoint[1] - 25, CAMERA.focalPoint[2]];
    arch.showPlane(onPlane);
    assert.deepEqual(references, [], 'the camera was already there; nothing to move');

    // And the upstream half that makes it so. A 5.9 that projects unconditionally would
    // make the guard merely redundant rather than load-bearing -- but a bump that removed
    // the projection altogether must fail here, not on a patient's axial.
    assert.match(
        readFileSync(
            'node_modules/@cornerstonejs/core/dist/esm/RenderingEngine/BaseVolumeViewport.js',
            'utf8'
        ),
        /const normalDot = vec3\.dot\(focalDelta, useNormal\);\s*if \(!isEqual\(normalDot, 0\)\) \{\s*vec3\.scale\(focalDelta, useNormal, normalDot\);/,
        'the in-plane delta is no longer left unprojected; re-derive showPlane\'s guard'
    );
});

test('an arch drawn on the plane the camera was moved to survives the slice filter', () => {
    // The two halves together, over the library's real filter: the annotation is kept only
    // while it is within half a slice of the focal point, so this is the assertion that
    // would have failed while `showSlice` was mirroring the plane.
    const affine = descriptor().affine;
    const sliceIndex = 3;
    const plane = indexToWorldLps(affine, [0, 0, sliceIndex]);
    const { added, arch } = archSurface();
    arch.setArch([[1, 1], [2, 1], [2, 0]], ([x, y]) => indexToWorldLps(affine, [x, y, sliceIndex]));

    const camera = { ...CAMERA, focalPoint: plane };
    assert.deepEqual(
        toolsUtilities.planar.filterAnnotationsWithinSlice(added, camera, 0.6),
        added
    );
    // And one slice away -- which is what the index mirror produced -- it is dropped, with
    // no warning anywhere.
    const offBy = { ...CAMERA, focalPoint: indexToWorldLps(affine, [0, 0, sliceIndex + 1]) };
    assert.deepEqual(toolsUtilities.planar.filterAnnotationsWithinSlice(added, offBy, 0.6), []);
});

test('the mask is blue where the mandible is and absent everywhere else', () => {
    // `MASK_COLOR` was declared and used by nothing: the mask was drawn through a
    // window/level, which is a greyscale ramp, and its zero voxels were painted as
    // translucent black over the whole slice.
    const bytes = maskRgba(Uint8Array.from([0, 1, 0, 0, 1, 1]), 6);
    const pixel = (index) => [...bytes.slice(index * 4, index * 4 + 4)];
    const blue = [
        ...MASK_COLOR.map((channel) => Math.round(channel * 255)),
        Math.round(MASK_OPACITY * 255),
    ];

    assert.deepEqual(pixel(1), blue);
    assert.deepEqual(pixel(4), blue);
    assert.deepEqual(pixel(5), blue);
    // Absent, not dark. A background painted as translucent black is what dimmed the whole
    // axial and left the mandible as the only untinted region.
    assert.deepEqual(pixel(0), [0, 0, 0, 0]);
    assert.deepEqual(pixel(3), [0, 0, 0, 0]);
});

test('the mask lands where the arch does, direction included', () => {
    const placement = maskPlacement({ descriptor: descriptor(), sliceIndex: 3 });

    // The origin is the world position of voxel (0, 0, slice) -- read through the very
    // function that places an arch control point.
    assert.deepEqual(placement.origin, indexToWorldLps(descriptor().affine, [0, 0, 3]));
    // Negative in-plane axes: this is the half an earlier version dropped by taking the
    // magnitude of the affine's columns, which mirrored the mask about the origin.
    // Rounded because these are differences of world positions, not the affine's own
    // entries, and `-0` is not `0` to a strict deep-equal.
    const round = (vector) => vector.map((value) => Number(value.toFixed(6)) || 0);
    assert.deepEqual(round(placement.axisI), [-0.4, 0, 0]);
    assert.deepEqual(round(placement.axisJ), [0, -0.5, 0]);

    // And the round trip: voxel (2, 1) has to land on the same world point the arch would.
    const at = (i, j) =>
        placement.origin.map((value, axis) => value + placement.axisI[axis] * i + placement.axisJ[axis] * j);
    assert.deepEqual(
        at(2, 1).map((v) => Number(v.toFixed(6))),
        indexToWorldLps(descriptor().affine, [2, 1, 3]).map((v) => Number(v.toFixed(6)))
    );
});

test('the mask follows the view through the viewport\'s own projection', () => {
    // Three projected points fix an affine mapping, so panning and zooming cost no pass
    // over the pixels. The half-pixel offsets are because a world position names a pixel's
    // centre and a canvas image starts at its corner.
    const placement = maskPlacement({ descriptor: descriptor(), sliceIndex: 3 });
    // A projection that flips y and scales by 2, so a wrong sign cannot pass by symmetry.
    const worldToCanvas = ([x, y]) => [2 * x, -2 * y];
    const [a, b, c, d, e, f] = maskCanvasTransform(placement, worldToCanvas);

    const same = (actual, expected) =>
        assert.deepEqual(
            actual.map((value) => Number(value.toFixed(6)) || 0),
            expected.map((value) => Number(value.toFixed(6)) || 0)
        );
    same([a, b], [2 * placement.axisI[0], -2 * placement.axisI[1]]);
    same([c, d], [2 * placement.axisJ[0], -2 * placement.axisJ[1]]);
    const corner = placement.origin.map(
        (value, axis) => value - placement.axisI[axis] / 2 - placement.axisJ[axis] / 2
    );
    same([e, f], worldToCanvas(corner));

    // A projection that has not settled answers with non-finite numbers; drawing through
    // that leaves the previous slice's mask on the overlay, so it is refused.
    assert.equal(maskCanvasTransform(placement, () => [NaN, NaN]), null);
    assert.equal(maskCanvasTransform(placement, () => [0, 0]), null, 'a collapsed basis too');
});

test('a first sizing refits the camera and puts it back on the arch plane', () => {
    // The stage is `hidden` when this viewport is enabled, so Cornerstone builds the
    // canvas at the HTML default 300x150 and fits a camera to it. Everything scaled to
    // that canvas is wrong once the editor is shown -- the copied picture, and
    // `worldToCanvas`, which is what places the mandible overlay and every control point
    // the tools draw. `resetCamera` throws the camera away; it also centres the focal
    // point on the volume, so the arch's plane has to be re-asserted or the axial comes
    // back on a different slice from the one the spline was fitted to.
    const { arch, references, resets } = archSurface();
    arch.showPlane([0, 0, 9]);
    assert.equal(references.length, 1);

    arch.reframe(true);
    assert.deepEqual(resets, [{ resetPan: true, resetZoom: true, resetToCenter: true }]);
    assert.equal(references.length, 2, 'the plane is named again after the refit');

    // A later resize keeps whatever the reader has panned and zoomed to.
    arch.reframe();
    assert.equal(resets.length, 1);
    assert.equal(references.length, 2);
});
