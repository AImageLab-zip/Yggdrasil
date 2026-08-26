/**
 * Reorienting a volume to RAS storage order, without NiiVue.
 *
 * The panoramic reconstruction (`static/js/modality_viewers/cbct_panorex_editor.js`)
 * does not read pixels off the screen; it reads the **voxel array** and the affine, and
 * bakes a MIP and a ray-sum strip from them. Today it gets both from NiiVue, via
 * `ViewerGrid.getNativeRawVolumeDescriptor()`, and NiiVue *reorients every volume to
 * RAS on load*. So the array the panoramic has always consumed is not in the file's
 * storage order -- it is permuted and flipped into RAS first.
 *
 * That makes this module the precondition for deleting NiiVue at all. Handing the
 * panoramic a Cornerstone volume directly would hand it file-order voxels under an
 * interface that has always meant RAS-order, and the failure would be silent: the
 * export still succeeds, the strip still looks like a jaw, and the geometry is
 * transposed. `common/export_catalog.py` ships those PNGs, so it would be a change to
 * an exported clinical artifact that nothing in the build would notice.
 *
 * The permutation is derived from the affine the same way NiiVue derives it, because
 * "the same way" is the requirement -- the panoramic was tuned against NiiVue's output
 * and a different-but-defensible convention is still a change. NiiVue's derivation, in
 * `nvimage/index.js`:
 *
 *   - for each **input** axis, find the output row its affine column dominates;
 *   - invert that into `perm`, indexed by **output** axis;
 *   - flip an output axis when its dominant coefficient is negative;
 *   - `dimsRAS = [dims[0], dims[perm[0]], dims[perm[1]], dims[perm[2]]]`.
 *
 * `frontend/imaging/validation/voxelSampling.js` already consumes the resulting
 * `permRAS` in the other direction, and its bijection test covers the index algebra;
 * this module produces the same value from the affine and applies it to the data.
 */

/**
 * Derive NiiVue's `permRAS` from a RAS affine.
 *
 * @param {number[][]} affine 4x4, `affine[row][col]`, mapping voxel indices to RAS mm.
 * @returns {number[]} three signed 1-based input-axis ids, indexed by output axis.
 */
export function rasPermutation(affine) {
    // For each input axis, which output row does its column dominate?
    const dominantRow = [0, 1, 2].map((col) => {
        let best = 0;
        for (let row = 1; row < 3; row += 1) {
            if (Math.abs(affine[row][col]) > Math.abs(affine[best][col])) {
                best = row;
            }
        }
        return best;
    });

    if (new Set(dominantRow).size !== 3) {
        throw new Error(
            `The affine is degenerate: input axes map to output rows ` +
                `${JSON.stringify(dominantRow)}, which is not a permutation. ` +
                'Two storage axes point the same anatomical way, so there is no ' +
                'reorientation that could be correct.'
        );
    }

    // Invert: perm indexed by output axis gives the input axis (1-based), signed
    // negative when that axis has to be flipped.
    const perm = [0, 0, 0];
    for (let inputAxis = 0; inputAxis < 3; inputAxis += 1) {
        const outputAxis = dominantRow[inputAxis];
        const coefficient = affine[outputAxis][inputAxis];
        perm[outputAxis] = (inputAxis + 1) * (coefficient < 0 ? -1 : 1);
    }
    return perm;
}

/**
 * The voxel counts after reorientation.
 *
 * @param {number[]} dims `[x, y, z]` in file order.
 * @param {number[]} permRAS
 * @returns {number[]} `[x, y, z]` in RAS order.
 */
export function reorientedDimensions(dims, permRAS) {
    return [0, 1, 2].map((axis) => dims[Math.abs(permRAS[axis]) - 1]);
}

/**
 * The affine that describes the reoriented volume.
 *
 * Its columns are the original columns, permuted and negated to match the data; the
 * origin moves to the corner that is now voxel (0,0,0), which for a flipped axis is the
 * far end of the original one. Getting the origin wrong is a whole-field-of-view
 * translation -- the anatomy is intact and in the wrong place, which is the failure
 * that looks least like a bug.
 *
 * @param {number[][]} affine 4x4 RAS affine in file order.
 * @param {number[]} dims `[x, y, z]` in file order.
 * @param {number[]} permRAS
 * @returns {number[][]} 4x4 RAS affine for the reoriented data.
 */
export function reorientedAffine(affine, dims, permRAS) {
    const out = [
        [0, 0, 0, affine[0][3]],
        [0, 0, 0, affine[1][3]],
        [0, 0, 0, affine[2][3]],
        [0, 0, 0, 1],
    ];

    for (let outputAxis = 0; outputAxis < 3; outputAxis += 1) {
        const inputAxis = Math.abs(permRAS[outputAxis]) - 1;
        const flipped = permRAS[outputAxis] < 0;
        const sign = flipped ? -1 : 1;

        for (let row = 0; row < 3; row += 1) {
            out[row][outputAxis] = affine[row][inputAxis] * sign;
        }
        if (flipped) {
            // Voxel 0 of a flipped axis is the original axis's last voxel, so the
            // origin shifts along the original column by its full extent.
            const extent = dims[inputAxis] - 1;
            for (let row = 0; row < 3; row += 1) {
                out[row][3] += affine[row][inputAxis] * extent;
            }
        }
    }
    return out;
}

/**
 * Reorient a volume's voxels into RAS storage order.
 *
 * Walks the **output** in memory order so the writes are sequential and only the reads
 * scatter; the reverse costs a cache miss per voxel, which on a 60-million-voxel CBCT
 * is the difference between a pause and a hang.
 *
 * @param {ArrayLike<number>} data voxels in file order, `x` fastest.
 * @param {number[]} dims `[x, y, z]` in file order.
 * @param {number[]} permRAS
 * @returns {{data: ArrayLike<number>, dims: number[]}}
 */
export function reorientToRas(data, dims, permRAS) {
    const expected = dims[0] * dims[1] * dims[2];
    if (data.length < expected) {
        throw new Error(
            `The volume holds ${data.length} voxels but its dimensions imply ${expected}. ` +
                'Reorienting a short array would read past the end and write zeros.'
        );
    }

    // The identity permutation is the common case -- most studies are stored RAS
    // already -- and copying 60 million voxels to change nothing is worth skipping.
    if (permRAS[0] === 1 && permRAS[1] === 2 && permRAS[2] === 3) {
        return { data, dims: [...dims] };
    }

    const outDims = reorientedDimensions(dims, permRAS);
    const out = new data.constructor(expected);

    // Per output axis: which input axis it reads, that axis's stride in the input, and
    // whether it counts backwards. Hoisted out of the loop -- inside it, this is three
    // more multiplies per voxel.
    const inputStride = [1, dims[0], dims[0] * dims[1]];
    const source = [0, 1, 2].map((axis) => {
        const inputAxis = Math.abs(permRAS[axis]) - 1;
        const flipped = permRAS[axis] < 0;
        const stride = inputStride[inputAxis];
        return {
            step: flipped ? -stride : stride,
            start: flipped ? (dims[inputAxis] - 1) * stride : 0,
        };
    });

    let cursor = 0;
    for (let z = 0; z < outDims[2]; z += 1) {
        const zOffset = source[2].start + z * source[2].step;
        for (let y = 0; y < outDims[1]; y += 1) {
            const yOffset = zOffset + source[1].start + y * source[1].step;
            for (let x = 0; x < outDims[0]; x += 1) {
                out[cursor] = data[yOffset + source[0].start + x * source[0].step];
                cursor += 1;
            }
        }
    }

    return { data: out, dims: outDims };
}

/**
 * Everything the panoramic needs, from a file-order volume and its header.
 *
 * @param {object} options
 * @param {ArrayLike<number>} options.data voxels in file order.
 * @param {number[]} options.dims `[x, y, z]` in file order.
 * @param {number[][]} options.affine 4x4 RAS affine in file order.
 * @returns {{data: ArrayLike<number>, dims: number[], affine: number[][], permRAS: number[]}}
 */
export function toRasVolume({ data, dims, affine }) {
    const permRAS = rasPermutation(affine);
    const reoriented = reorientToRas(data, dims, permRAS);
    return {
        data: reoriented.data,
        dims: reoriented.dims,
        affine: reorientedAffine(affine, dims, permRAS),
        permRAS,
    };
}
