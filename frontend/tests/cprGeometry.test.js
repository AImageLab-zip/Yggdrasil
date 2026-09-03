import test from 'node:test';
import assert from 'node:assert/strict';

import {
    ORIENTATION_COMPONENTS,
    archCenterline,
    polyline,
    sliceAxis,
    viewUpSign,
} from '../imaging/panoramic/cprGeometry.js';

/** A RAS affine with the given per-axis spacing and origin, axes unpermuted. */
function affineOf(spacing = [1, 1, 1], origin = [0, 0, 0]) {
    return [
        [spacing[0], 0, 0, origin[0]],
        [0, spacing[1], 0, origin[1]],
        [0, 0, spacing[2], origin[2]],
        [0, 0, 0, 1],
    ];
}

/**
 * An arch running along +x at constant y, with its slab laid along +y.
 *
 * Deliberately the simplest geometry whose three directions are all different, so a
 * transposed column in the orientation matrix cannot pass by symmetry -- the mistake the
 * Phase 3 harness shipped a vacuous test for.
 */
function straightArch({ columns = 4, y = 30, half = 20, samples = 41 } = {}) {
    const centerline = [];
    const slab = [];
    for (let index = 0; index < columns; index += 1) {
        const x = 10 + index * 5;
        centerline.push([x, y]);
        const column = [];
        for (let sample = 0; sample < samples; sample += 1) {
            column.push([x, y - half + (2 * half * sample) / (samples - 1)]);
        }
        slab.push(column);
    }
    return { centerline, slab };
}

test('the slice axis is read off the affine, direction and spacing together', () => {
    const { direction, spacing } = sliceAxis(affineOf([0.3, 0.3, 0.5]));

    assert.deepEqual(direction, [0, 0, 1]);
    assert.equal(spacing, 0.5);
});

test('centreline points are carried into LPS, not left in RAS', () => {
    const affine = affineOf([1, 1, 1], [5, 7, 9]);

    const { points } = archCenterline({
        geometry: straightArch({ columns: 2 }),
        sliceIndex: 12,
        rasAffine: affine,
        dims: [100, 80, 40],
    });

    // RAS would be (15, 37, 21); LPS negates the first two. Getting this wrong mirrors
    // the whole panoramic across two planes and still renders something plausible.
    assert.deepEqual([...points.slice(0, 3)], [-15, -37, 21]);
});

test('the orientation triad is across-the-strip, along-the-slab, and their cross', () => {
    const { orientations } = archCenterline({
        geometry: straightArch({ columns: 2 }),
        sliceIndex: 0,
        rasAffine: affineOf(),
        dims: [100, 80, 40],
    });

    const first = [...orientations.slice(0, ORIENTATION_COMPONENTS)];
    const across = first.slice(0, 3);
    const alongSlab = first.slice(3, 6);
    const third = first.slice(6, 9);

    // Across the strip is +Z: the strip's rows are slices, which is what makes its height
    // the volume's depth and what the baker's `outputMip[z * W + column]` already means.
    assert.deepEqual(across, [0, 0, 1]);
    // The slab runs from the arch's `high` line to its `low` line. In LPS that is -y.
    assert.deepEqual(alongSlab, [0, -1, 0]);
    // Right-handed, so `quat.fromMat3` reads a rotation rather than a reflection.
    // Normalized to shed signed zeros, which carry no direction.
    assert.deepEqual(third.map((value) => value + 0), [1, 0, 0]);
});

test('every orientation is an orthonormal right-handed rotation', () => {
    const { orientations } = archCenterline({
        geometry: straightArch({ columns: 6 }),
        sliceIndex: 3,
        rasAffine: affineOf([0.25, 0.25, 0.4]),
        dims: [100, 80, 40],
    });

    for (let index = 0; index < orientations.length; index += ORIENTATION_COMPONENTS) {
        const tuple = [...orientations.slice(index, index + ORIENTATION_COMPONENTS)];
        const columns = [tuple.slice(0, 3), tuple.slice(3, 6), tuple.slice(6, 9)];
        for (const column of columns) {
            assert.ok(Math.abs(Math.hypot(...column) - 1) < 1e-6, 'unit length');
        }
        for (const [a, b] of [[0, 1], [1, 2], [0, 2]]) {
            const dot = columns[a].reduce((sum, value, axis) => sum + value * columns[b][axis], 0);
            assert.ok(Math.abs(dot) < 1e-6, 'orthogonal');
        }
    }
});

test('the slab thickness is the one the baker integrates over, in millimetres', () => {
    // 20 voxels either side of the arch, on a 0.25 mm grid, is 10 mm of bone.
    const { slabThickness, slabSamples } = archCenterline({
        geometry: straightArch({ half: 20, samples: 41 }),
        sliceIndex: 0,
        rasAffine: affineOf([0.25, 0.25, 0.4]),
        dims: [100, 80, 40],
    });

    assert.ok(Math.abs(slabThickness - 10) < 1e-6);
    // 41 samples, which is `slabCoordinates(lines, 40)` -- the same count the endpoint
    // records in `FileRegistry.metadata['slab']`.
    assert.equal(slabSamples, 41);
});

test('the strip spans the whole volume in Z, centred on it', () => {
    const { width, centerPoint } = archCenterline({
        geometry: straightArch(),
        sliceIndex: 0,
        rasAffine: affineOf([0.25, 0.25, 0.4]),
        dims: [100, 80, 40],
    });

    assert.ok(Math.abs(width - 40 * 0.4) < 1e-6);
    // The arch sits at one slice; without a centre point the strip would be a band around
    // it rather than the full height of the volume.
    const expected = [-(99 / 2) * 0.25, -(79 / 2) * 0.25, (39 / 2) * 0.4];
    centerPoint.forEach((value, axis) => {
        assert.ok(Math.abs(value - expected[axis]) < 1e-9);
    });
});

test('the cell array is one open polyline through every point', () => {
    assert.deepEqual([...polyline(3)], [3, 0, 1, 2]);
});

test('an arch with fewer than two points is refused rather than rendered', () => {
    assert.throws(
        () => archCenterline({
            geometry: { centerline: [[1, 1]], slab: [[[1, 1], [1, 2]]] },
            sliceIndex: 0,
            rasAffine: affineOf(),
            dims: [10, 10, 10],
        }),
        /at least two arch points/
    );
});

test('a slab that does not match the centreline is refused', () => {
    assert.throws(
        () => archCenterline({
            geometry: { centerline: [[1, 1], [2, 2]], slab: [[[1, 1], [1, 2]]] },
            sliceIndex: 0,
            rasAffine: affineOf(),
            dims: [10, 10, 10],
        }),
        /one sample column per centreline point/
    );
});

test('the view is inverted for exactly the volumes the baker flips', () => {
    assert.equal(viewUpSign(false), 1);
    assert.equal(viewUpSign(true), -1);
});
