import test from 'node:test';
import assert from 'node:assert/strict';

import {
    rasPermutation,
    reorientToRas,
    reorientedAffine,
    reorientedDimensions,
    toRasVolume,
} from '../imaging/geometry/reorient.js';
import { affineToOrientation, indexToWorldRas } from '../imaging/geometry/orientation.js';

/** Build a RAS affine from signed direction columns, spacings and an origin. */
function affineOf(columns, spacing = [1, 1, 1], origin = [0, 0, 0]) {
    const affine = [
        [0, 0, 0, origin[0]],
        [0, 0, 0, origin[1]],
        [0, 0, 0, origin[2]],
        [0, 0, 0, 1],
    ];
    for (let col = 0; col < 3; col += 1) {
        for (let row = 0; row < 3; row += 1) {
            affine[row][col] = columns[col][row] * spacing[col];
        }
    }
    return affine;
}

const IDENTITY = affineOf([[1, 0, 0], [0, 1, 0], [0, 0, 1]]);

/** A volume whose every voxel encodes its own file-order index. */
function indexVolume(dims) {
    const [nx, ny, nz] = dims;
    const data = new Int32Array(nx * ny * nz);
    let cursor = 0;
    for (let z = 0; z < nz; z += 1) {
        for (let y = 0; y < ny; y += 1) {
            for (let x = 0; x < nx; x += 1) {
                // Distinct per voxel and decodable back into (x, y, z).
                data[cursor] = x + y * 100 + z * 10000;
                cursor += 1;
            }
        }
    }
    return data;
}

const decode = (value) => [value % 100, Math.floor(value / 100) % 100, Math.floor(value / 10000)];

// ---------------------------------------------------------------------------
// The permutation
// ---------------------------------------------------------------------------

test('an already-RAS volume permutes to the identity', () => {
    assert.deepEqual(rasPermutation(IDENTITY), [1, 2, 3]);
});

test('a flipped axis comes back negative', () => {
    assert.deepEqual(rasPermutation(affineOf([[-1, 0, 0], [0, 1, 0], [0, 0, 1]])), [-1, 2, 3]);
    assert.deepEqual(rasPermutation(affineOf([[-1, 0, 0], [0, -1, 0], [0, 0, -1]])), [-1, -2, -3]);
});

test('a permuted volume reports which input axis each output axis reads', () => {
    // Input x -> RAS y, input y -> RAS z, input z -> RAS x. So output x reads input z.
    const affine = affineOf([[0, 1, 0], [0, 0, 1], [1, 0, 0]]);
    assert.deepEqual(rasPermutation(affine), [3, 1, 2]);
    assert.deepEqual(reorientedDimensions([10, 12, 14], [3, 1, 2]), [14, 10, 12]);
});

test('an oblique affine permutes by its dominant axis, not by exact alignment', () => {
    // A 12-degree tilt about x: still unambiguously RAS-ordered.
    const radians = (12 * Math.PI) / 180;
    const c = Math.cos(radians);
    const s = Math.sin(radians);
    assert.deepEqual(rasPermutation(affineOf([[1, 0, 0], [0, c, s], [0, -s, c]])), [1, 2, 3]);
});

test('a degenerate affine is refused rather than permuted arbitrarily', () => {
    // Two storage axes pointing the same anatomical way: no reorientation is correct.
    const degenerate = affineOf([[1, 0, 0], [1, 0, 0], [0, 0, 1]]);
    assert.throws(() => rasPermutation(degenerate), /not a permutation/);
});

// ---------------------------------------------------------------------------
// The data
// ---------------------------------------------------------------------------

test('the identity permutation returns the same array, not a copy', () => {
    // A 60-million-voxel copy that changes nothing is worth skipping, and most studies
    // are stored RAS already.
    const data = indexVolume([4, 5, 6]);
    const result = reorientToRas(data, [4, 5, 6], [1, 2, 3]);
    assert.equal(result.data, data);
    assert.deepEqual(result.dims, [4, 5, 6]);
});

test('a flipped axis reverses that axis and leaves the others alone', () => {
    const dims = [4, 5, 6];
    const { data } = reorientToRas(indexVolume(dims), dims, [-1, 2, 3]);
    // Output (0, y, z) must hold input (3, y, z).
    assert.deepEqual(decode(data[0]), [3, 0, 0]);
    assert.deepEqual(decode(data[3]), [0, 0, 0]);
    // And the other axes are untouched: output (0,1,0) is input (3,1,0).
    assert.deepEqual(decode(data[4]), [3, 1, 0]);
});

test('a permutation moves every voxel to the axis it belongs on', () => {
    const dims = [4, 5, 6];
    const permRAS = [3, 1, 2]; // output x reads input z, output y reads x, output z reads y
    const { data, dims: outDims } = reorientToRas(indexVolume(dims), dims, permRAS);
    assert.deepEqual(outDims, [6, 4, 5]);

    // Output voxel (2, 1, 3) reads input (x=1, y=3, z=2).
    const index = 2 + 1 * outDims[0] + 3 * outDims[0] * outDims[1];
    assert.deepEqual(decode(data[index]), [1, 3, 2]);
});

test('reorientation is a bijection: every voxel survives exactly once', () => {
    const dims = [4, 5, 6];
    for (const permRAS of [[1, 2, 3], [-1, 2, 3], [3, 1, 2], [-3, 1, -2], [-1, -2, -3]]) {
        const { data } = reorientToRas(indexVolume(dims), dims, permRAS);
        assert.equal(new Set(data).size, dims[0] * dims[1] * dims[2], JSON.stringify(permRAS));
    }
});

test('a short array is refused rather than padded with zeros', () => {
    assert.throws(
        () => reorientToRas(new Int32Array(10), [4, 5, 6], [3, 1, 2]),
        /would read past the end/
    );
});

test('the output keeps the input array type', () => {
    const dims = [2, 2, 2];
    for (const Ctor of [Int16Array, Uint16Array, Float32Array]) {
        const { data } = reorientToRas(new Ctor(8), dims, [-1, 2, 3]);
        assert.ok(data instanceof Ctor, Ctor.name);
    }
});

// ---------------------------------------------------------------------------
// The affine must keep describing the data
// ---------------------------------------------------------------------------

test('the reoriented affine maps reoriented indices to the SAME world points', () => {
    // The property that makes the whole thing safe: reorientation moves voxels and
    // rewrites the affine so that the physical location of each voxel is unchanged.
    const dims = [4, 5, 6];
    const spacing = [0.3, 0.4, 0.5];
    const origin = [-12, 7, 30];

    for (const columns of [
        [[-1, 0, 0], [0, 1, 0], [0, 0, 1]],
        [[0, 1, 0], [0, 0, 1], [1, 0, 0]],
        [[0, -1, 0], [0, 0, 1], [-1, 0, 0]],
        [[-1, 0, 0], [0, -1, 0], [0, 0, -1]],
    ]) {
        const affine = affineOf(columns, spacing, origin);
        const permRAS = rasPermutation(affine);
        const outDims = reorientedDimensions(dims, permRAS);
        const outAffine = reorientedAffine(affine, dims, permRAS);

        // For a sample of output voxels, find the input voxel they came from and check
        // both affines put them in the same place.
        for (const out of [[0, 0, 0], [1, 2, 3], [outDims[0] - 1, outDims[1] - 1, outDims[2] - 1]]) {
            const input = [0, 0, 0];
            for (let axis = 0; axis < 3; axis += 1) {
                const inputAxis = Math.abs(permRAS[axis]) - 1;
                input[inputAxis] =
                    permRAS[axis] < 0 ? dims[inputAxis] - 1 - out[axis] : out[axis];
            }
            const before = indexToWorldRas(affine, input);
            const after = indexToWorldRas(outAffine, out);
            for (let axis = 0; axis < 3; axis += 1) {
                assert.ok(
                    Math.abs(before[axis] - after[axis]) < 1e-9,
                    `${JSON.stringify(columns)} out=${JSON.stringify(out)} axis ${axis}: ` +
                        `${before[axis]} vs ${after[axis]}`
                );
            }
        }
    }
});

test('the reoriented affine is RAS by construction', () => {
    for (const columns of [
        [[-1, 0, 0], [0, 1, 0], [0, 0, 1]],
        [[0, 1, 0], [0, 0, 1], [1, 0, 0]],
        [[-1, 0, 0], [0, -1, 0], [0, 0, -1]],
    ]) {
        const affine = affineOf(columns, [0.3, 0.4, 0.5], [-12, 7, 30]);
        const permRAS = rasPermutation(affine);
        assert.equal(
            affineToOrientation(reorientedAffine(affine, [4, 5, 6], permRAS)),
            'RAS',
            JSON.stringify(columns)
        );
    }
});

test('toRasVolume ties the three together', () => {
    const dims = [4, 5, 6];
    const affine = affineOf([[0, -1, 0], [0, 0, 1], [-1, 0, 0]], [0.3, 0.4, 0.5], [-12, 7, 30]);
    const result = toRasVolume({ data: indexVolume(dims), dims, affine });

    assert.deepEqual(result.dims, reorientedDimensions(dims, result.permRAS));
    assert.equal(affineToOrientation(result.affine), 'RAS');
    assert.equal(result.data.length, dims[0] * dims[1] * dims[2]);
    assert.equal(new Set(result.data).size, result.data.length);
});
