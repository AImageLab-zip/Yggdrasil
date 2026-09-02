import test from 'node:test';
import assert from 'node:assert/strict';

import vtkImageCPRMapper from '@kitware/vtk.js/Rendering/Core/ImageCPRMapper.js';
import { ProjectionMode } from '@kitware/vtk.js/Rendering/Core/ImageCPRMapper/Constants.js';
import vtkPolyData from '@kitware/vtk.js/Common/DataModel/PolyData.js';
import vtkDataArray from '@kitware/vtk.js/Common/Core/DataArray.js';
import vtkImageData from '@kitware/vtk.js/Common/DataModel/ImageData.js';

import {
    ORIENTATION_ARRAY_NAME,
    ORIENTATION_COMPONENTS,
} from '../imaging/panoramic/cprGeometry.js';
import { PROJECTION_MODES, createCprViewport } from '../imaging/panoramic/cprViewport.js';
import { readScalarData } from '../imaging/grid/volumeLoading.js';

/**
 * The panoramic's reformat is a *pinned* dependency on vtk.js, in the same way the
 * attenuated-MIP shader splice is (`attenuatedMip.test.js`) and the arch spline is on
 * Cornerstone (`archSpline.test.js`).
 *
 * `ImageCPRMapper` is configured entirely through setters and reads its per-point
 * orientation out of a point-data array *by name*. None of that is type-checked at runtime:
 * a renamed setter is `undefined is not a function` at mount, but a renamed **lookup** is
 * silent -- the mapper falls back to its uniform orientation and reformats along a straight
 * line, which on a dental arch is a plausible-looking image of the wrong anatomy.
 *
 * So a version bump that moves any of this must fail the build here rather than in front of
 * a clinician. **Do not delete this test**, and re-check the reformat on a real study if the
 * pin moves.
 */

test('every setter the strip is configured through still exists', () => {
    const mapper = vtkImageCPRMapper.newInstance();

    for (const name of [
        'setImageData',
        'setCenterlineData',
        'setWidth',
        'setCenterPoint',
        'setOrientationArrayName',
        'setUseUniformOrientation',
        'setProjectionMode',
        'setProjectionSlabThickness',
        'setProjectionSlabNumberOfSamples',
    ]) {
        assert.equal(typeof mapper[name], 'function', name);
    }
});

test('the projection modes are the ones the toolbar maps onto', () => {
    // MAX is what the baker does. AVERAGE is the closest vtk has to its clipped
    // non-negative sum -- named in `cprViewport.js` as the one place the live preview and
    // the stored artifact are not the same function of the voxels.
    assert.equal(PROJECTION_MODES.mip, ProjectionMode.MAX);
    assert.equal(PROJECTION_MODES.raysum, ProjectionMode.AVERAGE);
});

test('a nine-component point array is read as the per-point orientation', () => {
    const mapper = vtkImageCPRMapper.newInstance();
    mapper.setOrientationArrayName(ORIENTATION_ARRAY_NAME);
    mapper.setUseUniformOrientation(false);

    const centerline = vtkPolyData.newInstance();
    centerline.getPoints().setData(Float32Array.from([0, 0, 0, 1, 0, 0]), 3);
    centerline.getLines().setData(Uint32Array.from([2, 0, 1]));
    centerline.getPointData().addArray(
        vtkDataArray.newInstance({
            name: ORIENTATION_ARRAY_NAME,
            numberOfComponents: ORIENTATION_COMPONENTS,
            values: Float32Array.from([
                0, 0, 1, 0, -1, 0, 1, 0, 0,
                0, 0, 1, 0, -1, 0, 1, 0, 0,
            ]),
        })
    );
    mapper.setCenterlineData(centerline);

    // The name lookup is the silent half: `getOrientationDataArray` returning null makes
    // the mapper fall back to a uniform orientation and reformat along a straight line.
    assert.notEqual(mapper.getOrientationDataArray(), null);
    assert.equal(mapper.getOrientationDataArray().getNumberOfComponents(), ORIENTATION_COMPONENTS);

    // And nine components must still select the mat3 branch that turns them into
    // quaternions, rather than being reported as an unsupported shape.
    const oriented = mapper.getOrientedCenterline();
    assert.equal(oriented.getOrientations().length, 2);
    for (const quaternion of oriented.getOrientations()) {
        assert.equal(quaternion.length, 4);
        assert.ok(Math.abs(Math.hypot(...quaternion) - 1) < 1e-6, 'normalized');
    }
});

test('a centreline with no orientation is refused, not silently straightened', () => {
    const mapper = vtkImageCPRMapper.newInstance();
    mapper.setOrientationArrayName(ORIENTATION_ARRAY_NAME);
    mapper.setUseUniformOrientation(false);
    const centerline = vtkPolyData.newInstance();
    centerline.getPoints().setData(Float32Array.from([0, 0, 0, 1, 0, 0]), 3);
    centerline.getLines().setData(Uint32Array.from([2, 0, 1]));
    mapper.setCenterlineData(centerline);

    // Upstream resets to an empty oriented centreline rather than throwing, so this is
    // recorded as the behaviour to expect: the surface must supply the array, and does.
    assert.equal(mapper.getOrientedCenterline().getOrientations().length, 0);
});

test('the mapper reads voxels out of point-data scalars, which a Cornerstone volume has none of', () => {
    // The pin behind `cprViewport.setVolume`. `vtkImageCPRMapper.buildBufferObjects` does
    // `const scalars = image?.getPointData()?.getScalars(); if (!scalars) return;` and
    // leaves `volumeTexture` at its `null` default, which `updateBufferObjects`
    // dereferences one line later (`Rendering/OpenGL/ImageCPRMapper.js:99,106,119-125`).
    // A 5.8.2 `ImageVolume.imageData` carries a `voxelManager` and `hasScalarVolume:
    // false` instead of scalars, so handing it over threw -- and because
    // `ContextPoolRenderingEngine._renderFlaggedViewports` has no `try` and clears
    // `_animationFrameSet` only after its loop, that throw froze every viewport on the
    // shared engine, axial included.
    const bare = vtkImageData.newInstance();
    bare.setDimensions(2, 2, 2);
    bare.set({ voxelManager: { getScalarData: () => Int16Array.from([0]) } }, true);
    assert.equal(bare.getPointData().getScalars(), null, 'a voxelManager is not scalars');

    const mapper = vtkImageCPRMapper.newInstance();
    mapper.setImageData(bare);
    assert.equal(mapper.getInputData(0)?.getPointData()?.getScalars(), null);
});

test('setVolume gives the mapper an imageData that carries the voxels', () => {
    const values = Int16Array.from({ length: 2 * 3 * 4 }, (_unused, index) => index);
    const volume = {
        dimensions: [2, 3, 4],
        spacing: [0.5, 0.5, 1],
        direction: [1, 0, 0, 0, 1, 0, 0, 0, 1],
        origin: [-10, -20, -30],
        voxelManager: { getCompleteScalarDataArray: () => values },
        // Present, and deliberately unusable: the point of the fix is that this object is
        // *not* what the mapper is handed.
        imageData: vtkImageData.newInstance(),
    };

    let handed = null;
    const cpr = createCprViewport({
        element: {},
        cornerstone: {
            renderingEngine: {
                enableElement() {},
                getViewport: () => ({ addActor() {}, render() {}, removeActors() {} }),
            },
            coreEnums: { ViewportType: { VOLUME_3D: 'volume3d' } },
        },
        vtk: {
            vtkImageCPRMapper: {
                newInstance: () => ({
                    setOrientationArrayName() {},
                    setUseUniformOrientation() {},
                    setImageData: (imageData) => {
                        handed = imageData;
                    },
                }),
            },
            vtkImageSlice: { newInstance: () => ({ setMapper() {} }) },
            vtkPolyData,
            vtkDataArray,
            vtkImageData,
        },
    });

    cpr.setVolume(volume);

    assert.ok(handed, 'the mapper was given an imageData');
    assert.notEqual(handed, volume.imageData, "the volume's own imageData is left alone");
    const scalars = handed.getPointData().getScalars();
    assert.ok(scalars, 'point-data scalars are set, which is the whole fix');
    assert.equal(scalars.getNumberOfComponents(), 1);
    // Shared, not copied: a CBCT is not a thing to hold twice.
    assert.equal(scalars.getData(), values);
    assert.deepEqual(Array.from(handed.getDimensions()), volume.dimensions);
    assert.deepEqual(Array.from(handed.getSpacing()), volume.spacing);
    assert.deepEqual(Array.from(handed.getOrigin()), volume.origin);
});

test('setVolume refuses a volume that has not finished loading', () => {
    // `readScalarData`'s contract, reused rather than re-derived: an unread voxel manager
    // hands back an empty array instead of throwing, and reformatting padding would look
    // like anatomy.
    assert.throws(
        () => readScalarData({ voxelManager: { getCompleteScalarDataArray: () => Int16Array.of() } }),
        /cached no voxels/
    );
    assert.throws(() => readScalarData({}), /no voxel manager/);
});
