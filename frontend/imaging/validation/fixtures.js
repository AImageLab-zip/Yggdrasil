/**
 * Synthetic NIfTI-1 volumes, written byte by byte.
 *
 * The harness runs on real studies, which is the point -- but real studies cannot
 * cover the cases that matter most, because they are the cases nobody uploads on
 * purpose. There is no maxillo patient whose CBCT has `scl_slope = 2, scl_inter = 0`,
 * and hoping one turns up is not a test strategy. So the four branches of F1, the F2
 * undeclared-orientation case, and a known-chirality blob are constructed here and
 * driven through **the same loader path** as a real volume.
 *
 * Written as real files rather than as mock header objects for one reason: a mock
 * skips the parser, and the parser is part of what is being validated. A fixture that
 * round-trips through `nifti-reader-js` and through Cornerstone's own header reader is
 * evidence; a hand-built object literal is an assumption.
 *
 * Field offsets are NIfTI-1 (`nifti1.h`), cross-checked against the vendored reader in
 * `static/js/nifti-reader.js` rather than against documentation:
 *
 *     0   sizeof_hdr (348)     40  dim[8]            70  datatype
 *     72  bitpix                76  pixdim[8]        108  vox_offset (352)
 *     112 scl_slope            116  scl_inter        124  cal_max
 *     128 cal_min              252  qform_code       254  sform_code
 *     280 srow_x[4]            296  srow_y[4]        312  srow_z[4]
 *     344 magic ("n+1\0")      348  extension flag
 */

/** Where the voxel data starts in a single-file NIfTI-1. */
export const VOX_OFFSET = 352;

/** NIfTI datatype codes, with the typed array and bit width each implies. */
export const DATATYPES = Object.freeze({
    uint8: Object.freeze({ code: 2, bitpix: 8, Array: Uint8Array }),
    int16: Object.freeze({ code: 4, bitpix: 16, Array: Int16Array }),
    float32: Object.freeze({ code: 16, bitpix: 32, Array: Float32Array }),
    int8: Object.freeze({ code: 256, bitpix: 8, Array: Int8Array }),
    uint16: Object.freeze({ code: 512, bitpix: 16, Array: Uint16Array }),
});

/**
 * Build a complete single-file NIfTI-1 volume in memory.
 *
 * @param {object} options
 * @param {number[]} options.dims `[nx, ny, nz]`.
 * @param {number[]} [options.spacing] mm per voxel.
 * @param {string} [options.datatype] a key of {@link DATATYPES}.
 * @param {number} [options.sclSlope]
 * @param {number} [options.sclInter]
 * @param {number} [options.qformCode]
 * @param {number} [options.sformCode]
 * @param {number[][]} [options.affine] 4x4 RAS; the first three rows become srow_*.
 * @param {ArrayLike<number>} [options.voxels] raw stored values, length nx*ny*nz.
 * @param {number} [options.calMin]
 * @param {number} [options.calMax]
 * @returns {{buffer: ArrayBuffer, voxels: ArrayLike<number>, header: object}}
 */
export function buildNifti1({
    dims,
    spacing = [1, 1, 1],
    datatype = 'int16',
    sclSlope = 1,
    sclInter = 0,
    qformCode = 1,
    sformCode = 1,
    affine = null,
    voxels = null,
    calMin = 0,
    calMax = 0,
}) {
    const type = DATATYPES[datatype];
    if (!type) {
        throw new Error(`Unknown datatype '${datatype}'.`);
    }
    const [nx, ny, nz] = dims;
    const voxelCount = nx * ny * nz;
    const data = voxels ? type.Array.from(voxels) : new type.Array(voxelCount);
    if (data.length !== voxelCount) {
        throw new Error(`voxels has length ${data.length}, expected ${voxelCount}.`);
    }

    const matrix = affine ?? diagonalAffine(spacing);
    const buffer = new ArrayBuffer(VOX_OFFSET + data.byteLength);
    const view = new DataView(buffer);
    const LE = true;

    view.setInt32(0, 348, LE);
    view.setInt16(40, 3, LE); // dim[0]: three spatial dimensions
    view.setInt16(42, nx, LE);
    view.setInt16(44, ny, LE);
    view.setInt16(46, nz, LE);
    for (let slot = 4; slot < 8; slot += 1) {
        view.setInt16(40 + slot * 2, 1, LE);
    }
    view.setInt16(70, type.code, LE);
    view.setInt16(72, type.bitpix, LE);

    view.setFloat32(76, 1, LE); // pixdim[0] = qfac
    view.setFloat32(80, spacing[0], LE);
    view.setFloat32(84, spacing[1], LE);
    view.setFloat32(88, spacing[2], LE);

    view.setFloat32(108, VOX_OFFSET, LE);
    view.setFloat32(112, sclSlope, LE);
    view.setFloat32(116, sclInter, LE);
    view.setFloat32(124, calMax, LE);
    view.setFloat32(128, calMin, LE);

    view.setInt16(252, qformCode, LE);
    view.setInt16(254, sformCode, LE);

    // srow_x, srow_y, srow_z -- the sform affine, which is what the reader uses when
    // sform_code >= qform_code. The fixtures never rely on quaternion decoding.
    for (let row = 0; row < 3; row += 1) {
        for (let col = 0; col < 4; col += 1) {
            view.setFloat32(280 + row * 16 + col * 4, matrix[row][col], LE);
        }
    }

    // magic "n+1\0"
    for (const [slot, code] of [...'n+1'].entries()) {
        view.setUint8(344 + slot, code.charCodeAt(0));
    }
    view.setUint8(347, 0);
    // Extension flag: all zero, i.e. no extensions.

    new Uint8Array(buffer, VOX_OFFSET).set(new Uint8Array(data.buffer, data.byteOffset, data.byteLength));

    return {
        buffer,
        voxels: data,
        header: {
            dims: [3, nx, ny, nz, 1, 1, 1, 1],
            pixDims: [1, spacing[0], spacing[1], spacing[2], 1, 1, 1, 1],
            datatypeCode: type.code,
            numBitsPerVoxel: type.bitpix,
            scl_slope: sclSlope,
            scl_inter: sclInter,
            cal_min: calMin,
            cal_max: calMax,
            qform_code: qformCode,
            sform_code: sformCode,
            affine: matrix,
        },
    };
}

/** A diagonal RAS affine with the given spacings and a zero origin. */
export function diagonalAffine(spacing, origin = [0, 0, 0]) {
    return [
        [spacing[0], 0, 0, origin[0]],
        [0, spacing[1], 0, origin[1]],
        [0, 0, spacing[2], origin[2]],
        [0, 0, 0, 1],
    ];
}

/**
 * A deterministic, structured voxel pattern.
 *
 * Not noise: a gradient plus a dense cuboid gives the histogram a bulk and a tail, so
 * the robust range has something to exclude, and gives Tier 2's stratified walk a
 * reason to visit every region. Deterministic so two runs produce identical fixtures.
 *
 * @param {number[]} dims
 * @param {object} [options]
 * @param {number} [options.base] value at the origin corner.
 * @param {number} [options.gradient] added per voxel along the diagonal.
 * @param {number} [options.dense] value inside the cuboid.
 * @returns {Int32Array} stored values, before any rescale.
 */
export function gradientVolume(dims, { base = 0, gradient = 1, dense = null } = {}) {
    const [nx, ny, nz] = dims;
    const data = new Int32Array(nx * ny * nz);
    const denseValue = dense ?? base + gradient * (nx + ny + nz) * 2;

    let cursor = 0;
    for (let k = 0; k < nz; k += 1) {
        for (let j = 0; j < ny; j += 1) {
            for (let i = 0; i < nx; i += 1) {
                const inDenseRegion =
                    i >= Math.floor(nx * 0.6) &&
                    i < Math.floor(nx * 0.8) &&
                    j >= Math.floor(ny * 0.6) &&
                    j < Math.floor(ny * 0.8) &&
                    k >= Math.floor(nz * 0.6) &&
                    k < Math.floor(nz * 0.8);
                data[cursor] = inDenseRegion ? denseValue : base + gradient * (i + j + k);
                cursor += 1;
            }
        }
    }
    return data;
}

/**
 * The four rescale branches of F1, as loadable volumes.
 *
 * `upstreamSkips` records what `modalityScaleNifti` will do with each, so a harness
 * run can assert the prediction as well as the outcome -- if upstream ever fixes the
 * `&&`, these fixtures are how we find out, rather than by a silent doubling.
 *
 * @param {number[]} [dims] kept small; these run in a browser tab.
 * @returns {object[]}
 */
export function rescaleBranchFixtures(dims = [16, 16, 16]) {
    const voxels = gradientVolume(dims, { base: 100, gradient: 7 });
    return [
        {
            name: 'scl (1, -1024) -- the ordinary uint16-plus-intercept CT/CBCT encoding',
            upstreamSkips: true,
            upstreamWrong: true,
            expectation: 'every cached voxel is 1024 HU too high until the residual LUT is applied',
            ...buildNifti1({ dims, datatype: 'uint16', sclSlope: 1, sclInter: -1024, voxels }),
        },
        {
            name: 'scl (2, 0) -- pure gain, no offset',
            upstreamSkips: true,
            upstreamWrong: true,
            expectation: 'every cached voxel is half its true value until the residual LUT is applied',
            ...buildNifti1({ dims, datatype: 'int16', sclSlope: 2, sclInter: 0, voxels }),
        },
        {
            name: 'scl (1, 0) -- already in modality units',
            upstreamSkips: true,
            upstreamWrong: false,
            expectation: 'skipping an identity rescale is harmless; cached equals raw',
            ...buildNifti1({ dims, datatype: 'int16', sclSlope: 1, sclInter: 0, voxels }),
        },
        {
            name: 'scl (0.5, -100) -- the only branch upstream gets right',
            upstreamSkips: false,
            upstreamWrong: false,
            expectation: 'upstream applies the rescale, so the residual LUT must be identity',
            ...buildNifti1({ dims, datatype: 'int16', sclSlope: 0.5, sclInter: -100, voxels }),
        },
    ];
}

/**
 * The F2 fixture: a volume that declares no orientation at all.
 *
 * `qform_code = sform_code = 0`, so `nifti-reader-js` fabricates a diagonal affine
 * from `pixDims` and asserts RAS storage order with no evidence. The point of shipping
 * it deliberately broken is that the harness must *say so* rather than rendering it
 * confidently -- a green Tier 1 on this fixture with no warning is itself the failure.
 *
 * @param {number[]} [dims]
 * @returns {object}
 */
export function undeclaredOrientationFixture(dims = [16, 16, 16]) {
    const spacing = [0.3, 0.3, 0.4];
    return {
        name: 'qform_code = sform_code = 0 -- orientation inferred from pixel dimensions',
        expectation:
            'both viewers agree, and both are guessing; the report must carry the F2 warning',
        expectsWarning: true,
        ...buildNifti1({
            dims,
            spacing,
            datatype: 'int16',
            qformCode: 0,
            sformCode: 0,
            // Deliberately anisotropic: a cube would let a permuted axis pass unnoticed.
            voxels: gradientVolume(dims, { base: -1000, gradient: 11 }),
        }),
    };
}

/**
 * A blob at a known RAS position, for the chirality check.
 *
 * Pure affine arithmetic cannot catch a mirroring introduced *downstream* of the
 * affine -- in a shader, a texture upload, a slice order. A single asymmetric blob at
 * a known side can: if it renders on the left, the volume is mirrored, and no amount
 * of matrix algebra would have said so.
 *
 * The blob sits well to the +x (patient **right**) side, off-centre in y and z too so
 * that a single-axis flip and an axis swap look different from each other.
 *
 * @param {number[]} [dims]
 * @returns {object}
 */
export function chiralityFixture(dims = [32, 24, 20]) {
    const [nx, ny, nz] = dims;
    const spacing = [1, 1, 1];
    const voxels = new Int32Array(nx * ny * nz).fill(-1000);

    // Voxel-space box, deliberately not centred on any axis.
    const box = {
        i: [Math.floor(nx * 0.72), Math.floor(nx * 0.88)],
        j: [Math.floor(ny * 0.55), Math.floor(ny * 0.7)],
        k: [Math.floor(nz * 0.3), Math.floor(nz * 0.45)],
    };
    for (let k = box.k[0]; k < box.k[1]; k += 1) {
        for (let j = box.j[0]; j < box.j[1]; j += 1) {
            for (let i = box.i[0]; i < box.i[1]; i += 1) {
                voxels[i + j * nx + k * nx * ny] = 2000;
            }
        }
    }

    // Origin puts the volume centre at the world origin, so the blob's RAS x is
    // unambiguously positive and "right" is a fact rather than a convention.
    const origin = [-(nx - 1) / 2, -(ny - 1) / 2, -(nz - 1) / 2];
    const centroidVoxel = [
        (box.i[0] + box.i[1] - 1) / 2,
        (box.j[0] + box.j[1] - 1) / 2,
        (box.k[0] + box.k[1] - 1) / 2,
    ];

    return {
        name: 'chirality blob -- a dense box on the patient right (+x RAS)',
        expectation: 'the blob centroid must resolve to positive RAS x; negative means mirrored',
        centroidVoxel,
        centroidRas: [0, 1, 2].map((axis) => centroidVoxel[axis] * spacing[axis] + origin[axis]),
        expectedSide: 'right',
        ...buildNifti1({
            dims,
            spacing,
            datatype: 'int16',
            affine: diagonalAffine(spacing, origin),
            voxels,
        }),
    };
}

/**
 * Every synthetic fixture the harness runs, in the order it reports them.
 *
 * @param {number[]} [dims]
 * @returns {object[]}
 */
export function allFixtures(dims) {
    return [
        ...rescaleBranchFixtures(dims),
        undeclaredOrientationFixture(dims),
        chiralityFixture(),
    ];
}
