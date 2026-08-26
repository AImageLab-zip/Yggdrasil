/**
 * Choosing which voxels to compare, and naming the same voxel in two index spaces.
 *
 * Two separate problems live here, and the second is the one that makes Tier 1 hard.
 *
 * **Sampling.** Pseudo-random interior points find a rotation or a sign error, but
 * they almost never land on the places where an off-by-one or an origin-convention
 * error shows up: the corners, the centre, and the last voxel along each axis. So the
 * sample set is deliberately *not* purely random -- {@link sampleVoxelIndices} pins
 * the landmarks first and fills the rest pseudo-randomly from a seeded generator.
 *
 * **Naming.** Cornerstone indexes the volume in the file's own storage order.
 * **NiiVue does not**: it reorients every volume to RAS on load, so `nv.volumes[0]`
 * has its own `dimsRAS` and its own index space, and voxel `(10, 20, 30)` in one is
 * generally not voxel `(10, 20, 30)` in the other. Comparing `frac2mm` against
 * `indexToWorld` without accounting for that compares two different voxels and reports
 * a geometry error that is really a bookkeeping error -- or, worse, agrees by accident
 * on a volume that happens to be stored RAS already, which is most of them, and so
 * passes on the corpus and fails on the one study that is not.
 *
 * {@link fileVoxelToRasVoxel} is the mapping, derived from `nvImage.permRAS`. The
 * convention is read off `@niivue/niivue@0.69.0` rather than assumed:
 *
 *   - `dimsRAS = [dims[0], dims[perm[0]], dims[perm[1]], dims[perm[2]]]`
 *     (`dist/index.js`, in the RAS reorientation), so **output axis `j` reads from
 *     input axis `perm[j] - 1`**.
 *   - `permRAS[j]` is that same `perm[j]`, negated when the axis also had to be
 *     flipped, so a negative entry means the index counts from the far end.
 *
 * Everything here is pure and index-only: no NiiVue import, no Cornerstone import. The
 * adapters pass the two numbers (`permRAS`, `dims`) in.
 */

import { mulberry32, DEFAULT_SEED } from './prng.js';

/** Tier 1's sample count, per the roadmap ("~10^4 pseudo-random voxel indices"). */
export const DEFAULT_SAMPLE_COUNT = 10000;

/**
 * The indices that must always be checked, whatever the random draw produces.
 *
 * The eight corners bound the volume, so any sign or handedness error shows up there
 * at maximum magnitude; the centre is where a symmetric error would cancel and is
 * therefore the one point a corners-only check could miss.
 *
 * @param {number[]} dims `[x, y, z]` voxel counts.
 * @returns {number[][]}
 */
export function landmarkVoxelIndices(dims) {
    const [nx, ny, nz] = dims;
    const last = [nx - 1, ny - 1, nz - 1];
    const corners = [];
    for (const i of [0, last[0]]) {
        for (const j of [0, last[1]]) {
            for (const k of [0, last[2]]) {
                corners.push([i, j, k]);
            }
        }
    }
    corners.push([Math.floor(nx / 2), Math.floor(ny / 2), Math.floor(nz / 2)]);
    return corners;
}

/**
 * A deterministic sample of voxel indices: the landmarks, then pseudo-random fill.
 *
 * @param {number[]} dims `[x, y, z]` voxel counts.
 * @param {object} [options]
 * @param {number} [options.count] total indices to return.
 * @param {number} [options.seed]
 * @returns {number[][]} integer `[i, j, k]` triples, all inside the volume.
 */
export function sampleVoxelIndices(dims, { count = DEFAULT_SAMPLE_COUNT, seed = DEFAULT_SEED } = {}) {
    if (dims.some((size) => !Number.isInteger(size) || size < 1)) {
        throw new Error(`dims must be positive integers, got ${JSON.stringify(dims)}.`);
    }
    const samples = landmarkVoxelIndices(dims).slice(0, count);
    const random = mulberry32(seed);
    while (samples.length < count) {
        samples.push(dims.map((size) => Math.min(size - 1, Math.floor(random() * size))));
    }
    return samples;
}

/**
 * Translate a file-storage voxel index into NiiVue's RAS-reoriented index space.
 *
 * @param {number[]} ijk index in the file's own storage order.
 * @param {object} volume
 * @param {number[]} volume.permRAS `nvImage.permRAS`, three signed 1-based axis ids.
 * @param {number[]} volume.dims `[x, y, z]` voxel counts in **file** order.
 * @returns {number[]} index in NiiVue's RAS order.
 */
export function fileVoxelToRasVoxel(ijk, { permRAS, dims }) {
    assertPermRAS(permRAS);
    const out = [0, 0, 0];
    for (let axis = 0; axis < 3; axis += 1) {
        const source = Math.abs(permRAS[axis]) - 1;
        const value = ijk[source];
        // A negative entry means this axis was flipped during reorientation, so the
        // index counts from the far end of the *source* axis -- whose length is
        // dims[source], not dims[axis]. Using the wrong one is silent on an isotropic
        // cube and wrong on every real CBCT.
        out[axis] = permRAS[axis] < 0 ? dims[source] - 1 - value : value;
    }
    return out;
}

/**
 * The RAS voxel counts NiiVue derives, given the file's counts and the permutation.
 *
 * Useful as a cross-check: an adapter that reports a `dimsRAS` disagreeing with this
 * has misread `permRAS`, and every index it produces afterwards is meaningless.
 *
 * @param {number[]} dims `[x, y, z]` in file order.
 * @param {number[]} permRAS
 * @returns {number[]} `[x, y, z]` in RAS order.
 */
export function rasDimensions(dims, permRAS) {
    assertPermRAS(permRAS);
    return [0, 1, 2].map((axis) => dims[Math.abs(permRAS[axis]) - 1]);
}

/**
 * Convert a voxel index to the fractional coordinate NiiVue's `frac2mm` expects.
 *
 * `convertFrac2Vox` in `@niivue/niivue@0.69.0` is
 * `Math.round(frac * dims - 0.5)`, so the inverse is `(voxel + 0.5) / dims`: NiiVue's
 * fractions address voxel **centres**, not corners. Getting this wrong is a uniform
 * half-voxel offset -- 0.15 mm on a typical CBCT, which is invisible on screen and
 * larger than Tier 1's 1e-4 mm tolerance by three orders of magnitude.
 *
 * @param {number[]} rasIjk index in NiiVue's RAS order.
 * @param {number[]} dimsRAS `[x, y, z]` voxel counts in RAS order.
 * @returns {number[]} three fractions, each in (0, 1).
 */
export function voxelToFraction(rasIjk, dimsRAS) {
    return [0, 1, 2].map((axis) => (rasIjk[axis] + 0.5) / dimsRAS[axis]);
}

function assertPermRAS(permRAS) {
    if (!Array.isArray(permRAS) || permRAS.length < 3) {
        throw new Error(`permRAS must be three signed axis ids, got ${JSON.stringify(permRAS)}.`);
    }
    const axes = permRAS.slice(0, 3).map((entry) => Math.abs(entry));
    if (!axes.every((axis) => axis === 1 || axis === 2 || axis === 3)) {
        throw new Error(`permRAS entries must be +/-1, +/-2 or +/-3, got ${JSON.stringify(permRAS)}.`);
    }
    if (new Set(axes).size !== 3) {
        throw new Error(`permRAS must be a permutation, got ${JSON.stringify(permRAS)}.`);
    }
}
