import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { DEFAULT_SEED, mulberry32 } from '../imaging/validation/prng.js';
import {
    DEFAULT_SAMPLE_COUNT,
    fileVoxelToRasVoxel,
    landmarkVoxelIndices,
    rasDimensions,
    sampleVoxelIndices,
    voxelToFraction,
} from '../imaging/validation/voxelSampling.js';
import {
    GEOMETRY_TOLERANCE,
    POSITION_TOLERANCE_MM,
    checkAnalyticLengths,
    compareDerivedGeometry,
    comparePositions,
    runTier1,
} from '../imaging/validation/tier1Geometry.js';
import { runTier2, toleranceFor } from '../imaging/validation/tier2Intensity.js';
import {
    DATATYPES,
    VOX_OFFSET,
    allFixtures,
    buildNifti1,
    chiralityFixture,
    diagonalAffine,
    gradientVolume,
    rescaleBranchFixtures,
    undeclaredOrientationFixture,
} from '../imaging/validation/fixtures.js';
import { describeGeometry, indexToWorldRas, rasToLps } from '../imaging/geometry/orientation.js';

// The real parser, so the fixtures are validated against the code that will read them
// rather than against the offsets I believe I wrote.
const require = createRequire(import.meta.url);
const nifti = require('nifti-reader-js');

// ---------------------------------------------------------------------------
// The fixtures are real NIfTI files
// ---------------------------------------------------------------------------

test('every fixture is a NIfTI-1 file the real reader accepts', () => {
    for (const fixture of allFixtures()) {
        assert.ok(nifti.isNIFTI1(fixture.buffer), `${fixture.name} is not NIfTI-1`);
        assert.ok(nifti.isNIFTI(fixture.buffer), fixture.name);
        assert.equal(nifti.isCompressed(fixture.buffer), false, fixture.name);
    }
});

test('the reader recovers every header field the fixtures set', () => {
    for (const fixture of allFixtures()) {
        const header = nifti.readHeader(fixture.buffer);
        const expected = fixture.header;

        assert.deepEqual(
            [header.dims[1], header.dims[2], header.dims[3]],
            [expected.dims[1], expected.dims[2], expected.dims[3]],
            `${fixture.name}: dims`
        );
        assert.equal(header.datatypeCode, expected.datatypeCode, `${fixture.name}: datatype`);
        assert.equal(header.numBitsPerVoxel, expected.numBitsPerVoxel, `${fixture.name}: bitpix`);
        assert.equal(header.scl_slope, expected.scl_slope, `${fixture.name}: scl_slope`);
        assert.equal(header.scl_inter, expected.scl_inter, `${fixture.name}: scl_inter`);
        assert.equal(header.qform_code, expected.qform_code, `${fixture.name}: qform_code`);
        assert.equal(header.sform_code, expected.sform_code, `${fixture.name}: sform_code`);
        assert.equal(header.vox_offset, VOX_OFFSET, `${fixture.name}: vox_offset`);

        for (let axis = 1; axis <= 3; axis += 1) {
            assert.ok(
                Math.abs(header.pixDims[axis] - expected.pixDims[axis]) < 1e-6,
                `${fixture.name}: pixDims[${axis}]`
            );
        }
    }
});

test('the reader recovers every voxel the fixtures wrote', () => {
    for (const fixture of allFixtures()) {
        const header = nifti.readHeader(fixture.buffer);
        const image = nifti.readImage(header, fixture.buffer);
        const Constructor = Object.values(DATATYPES).find(
            (type) => type.code === header.datatypeCode
        ).Array;
        const read = new Constructor(image);

        assert.equal(read.length, fixture.voxels.length, `${fixture.name}: voxel count`);
        for (let index = 0; index < read.length; index += 1) {
            if (read[index] !== fixture.voxels[index]) {
                assert.fail(
                    `${fixture.name}: voxel ${index} read back as ${read[index]}, wrote ${fixture.voxels[index]}`
                );
            }
        }
    }
});

test('a declared fixture round-trips its affine through the reader', () => {
    const spacing = [0.3, 0.4, 0.5];
    const origin = [-12.5, 7.25, 30];
    const fixture = buildNifti1({
        dims: [4, 5, 6],
        spacing,
        affine: diagonalAffine(spacing, origin),
    });
    const header = nifti.readHeader(fixture.buffer);

    for (let row = 0; row < 3; row += 1) {
        for (let col = 0; col < 4; col += 1) {
            assert.ok(
                Math.abs(header.affine[row][col] - fixture.header.affine[row][col]) < 1e-6,
                `affine[${row}][${col}]: ${header.affine[row][col]}`
            );
        }
    }
    assert.equal(describeGeometry(header).axcodes, 'RAS');
});

test('the F2 fixture makes the reader fabricate a diagonal affine, and is flagged', () => {
    const fixture = undeclaredOrientationFixture();
    const header = nifti.readHeader(fixture.buffer);

    assert.equal(header.qform_code, 0);
    assert.equal(header.sform_code, 0);
    // nifti-reader-js:701-704 -- pixDims onto the diagonal, positive signs, i.e. an
    // unevidenced assumption of RAS storage order.
    assert.ok(Math.abs(header.affine[0][0] - 0.3) < 1e-6);
    assert.ok(Math.abs(header.affine[1][1] - 0.3) < 1e-6);
    assert.ok(Math.abs(header.affine[2][2] - 0.4) < 1e-6);

    const described = describeGeometry(header);
    assert.equal(described.declared, false);
    assert.equal(described.hasMetadata, false);
    assert.match(described.issues.join(' '), /F2/);
});

test('the chirality fixture puts its blob on the patient right', () => {
    const fixture = chiralityFixture();
    const header = nifti.readHeader(fixture.buffer);
    const world = indexToWorldRas(header.affine, fixture.centroidVoxel);

    assert.ok(world[0] > 0, `blob centroid RAS x should be positive, got ${world[0]}`);
    assert.deepEqual(
        world.map((value) => Number(value.toFixed(6))),
        fixture.centroidRas.map((value) => Number(value.toFixed(6)))
    );

    // And it really is asymmetric: mirroring x must move it.
    const mirrored = indexToWorldRas(header.affine, [
        header.dims[1] - 1 - fixture.centroidVoxel[0],
        fixture.centroidVoxel[1],
        fixture.centroidVoxel[2],
    ]);
    assert.ok(mirrored[0] < 0, 'a mirrored blob must land on the other side');
});

test('gradientVolume is deterministic and has a bulk plus a dense tail', () => {
    const a = gradientVolume([8, 8, 8], { base: 0, gradient: 3 });
    const b = gradientVolume([8, 8, 8], { base: 0, gradient: 3 });
    assert.deepEqual(Array.from(a), Array.from(b));
    assert.ok(Math.max(...a) > Math.min(...a), 'the volume must not be constant');
    const dense = Array.from(a).filter((value) => value === Math.max(...a)).length;
    assert.ok(dense > 0 && dense < a.length / 2, 'the dense region must be a tail, not the bulk');
});

// ---------------------------------------------------------------------------
// Determinism
// ---------------------------------------------------------------------------

test('the PRNG is reproducible and stays in range', () => {
    const first = Array.from({ length: 500 }, mulberry32(DEFAULT_SEED));
    const second = Array.from({ length: 500 }, mulberry32(DEFAULT_SEED));
    assert.deepEqual(first, second);
    assert.ok(first.every((value) => value >= 0 && value < 1));
    // A different seed must actually differ, or the seed is decorative.
    assert.notDeepEqual(first, Array.from({ length: 500 }, mulberry32(DEFAULT_SEED + 1)));
});

test('sampling is deterministic, in bounds, and always covers the landmarks', () => {
    const dims = [37, 41, 23];
    const samples = sampleVoxelIndices(dims, { count: 400 });
    assert.deepEqual(samples, sampleVoxelIndices(dims, { count: 400 }));
    assert.equal(samples.length, 400);

    for (const [i, j, k] of samples) {
        assert.ok(Number.isInteger(i) && i >= 0 && i < dims[0]);
        assert.ok(Number.isInteger(j) && j >= 0 && j < dims[1]);
        assert.ok(Number.isInteger(k) && k >= 0 && k < dims[2]);
    }

    // The eight corners and the centre lead the list -- an off-by-one at the far edge
    // is exactly what a purely random interior draw would miss.
    const landmarks = landmarkVoxelIndices(dims);
    assert.equal(landmarks.length, 9);
    for (const landmark of landmarks) {
        assert.ok(
            samples.some((sample) => sample.every((value, axis) => value === landmark[axis])),
            `landmark ${JSON.stringify(landmark)} missing from the sample`
        );
    }
});

test('sampling refuses a degenerate volume rather than sampling nothing', () => {
    assert.throws(() => sampleVoxelIndices([0, 10, 10]), /positive integers/);
    assert.throws(() => sampleVoxelIndices([10, 10, 1.5]), /positive integers/);
});

test('the default sample count is the ~10^4 the roadmap specifies', () => {
    assert.equal(DEFAULT_SAMPLE_COUNT, 10000);
});

// ---------------------------------------------------------------------------
// NiiVue's reoriented index space
// ---------------------------------------------------------------------------

test('an identity permutation leaves voxel indices alone', () => {
    const dims = [10, 12, 14];
    assert.deepEqual(fileVoxelToRasVoxel([3, 4, 5], { permRAS: [1, 2, 3], dims }), [3, 4, 5]);
    assert.deepEqual(rasDimensions(dims, [1, 2, 3]), dims);
});

test('a negative permRAS entry counts that axis from the far end', () => {
    const dims = [10, 12, 14];
    // Axis 0 flipped: index 3 becomes 10 - 1 - 3 = 6.
    assert.deepEqual(fileVoxelToRasVoxel([3, 4, 5], { permRAS: [-1, 2, 3], dims }), [6, 4, 5]);
    // All three flipped.
    assert.deepEqual(fileVoxelToRasVoxel([0, 0, 0], { permRAS: [-1, -2, -3], dims }), [9, 11, 13]);
});

test('a permutation reads output axis j from input axis |permRAS[j]| - 1', () => {
    const dims = [10, 12, 14];
    // Output x <- input z, output y <- input x, output z <- input y.
    assert.deepEqual(fileVoxelToRasVoxel([1, 2, 3], { permRAS: [3, 1, 2], dims }), [3, 1, 2]);
    assert.deepEqual(rasDimensions(dims, [3, 1, 2]), [14, 10, 12]);
});

test('a flipped permuted axis uses the SOURCE axis length, not its own', () => {
    // The bug this pins is silent on a cube and wrong on every real CBCT: output axis 0
    // reads input axis 2, so the flip must be against dims[2] = 14, giving 14-1-3 = 10.
    const dims = [10, 12, 14];
    assert.deepEqual(fileVoxelToRasVoxel([1, 2, 3], { permRAS: [-3, 1, 2], dims }), [10, 1, 2]);
});

test('the index mapping is a bijection over the whole volume', () => {
    const dims = [5, 6, 7];
    for (const permRAS of [[1, 2, 3], [-2, 3, -1], [3, -1, 2], [-1, -2, -3]]) {
        const seen = new Set();
        const target = rasDimensions(dims, permRAS);
        for (let i = 0; i < dims[0]; i += 1) {
            for (let j = 0; j < dims[1]; j += 1) {
                for (let k = 0; k < dims[2]; k += 1) {
                    const out = fileVoxelToRasVoxel([i, j, k], { permRAS, dims });
                    out.forEach((value, axis) => {
                        assert.ok(
                            value >= 0 && value < target[axis],
                            `${JSON.stringify(permRAS)}: ${value} out of range on axis ${axis}`
                        );
                    });
                    seen.add(out.join(','));
                }
            }
        }
        assert.equal(seen.size, dims[0] * dims[1] * dims[2], `${JSON.stringify(permRAS)} is not a bijection`);
    }
});

test('a malformed permRAS is refused, not quietly half-applied', () => {
    const dims = [4, 4, 4];
    assert.throws(() => fileVoxelToRasVoxel([0, 0, 0], { permRAS: [1, 2], dims }), /three signed axis ids/);
    assert.throws(() => fileVoxelToRasVoxel([0, 0, 0], { permRAS: [1, 2, 4], dims }), /must be/);
    assert.throws(() => fileVoxelToRasVoxel([0, 0, 0], { permRAS: [1, 1, 2], dims }), /permutation/);
});

test('voxelToFraction inverts NiiVue convertFrac2Vox, at voxel centres', () => {
    const dimsRAS = [10, 20, 40];
    for (const voxel of [[0, 0, 0], [5, 10, 20], [9, 19, 39]]) {
        const frac = voxelToFraction(voxel, dimsRAS);
        // `convertFrac2Vox` is Math.round(frac * dims - 0.5); the round trip must be exact.
        const back = frac.map((value, axis) => Math.round(value * dimsRAS[axis] - 0.5));
        assert.deepEqual(back, voxel);
        assert.ok(frac.every((value) => value > 0 && value < 1));
    }
    // Explicitly: centres, not corners. Voxel 0 is at half a voxel, not at zero.
    assert.deepEqual(voxelToFraction([0, 0, 0], [10, 10, 10]), [0.05, 0.05, 0.05]);
});

// ---------------------------------------------------------------------------
// Tier 1
// ---------------------------------------------------------------------------

const TILTED = (() => {
    const radians = (9 * Math.PI) / 180;
    const c = Math.cos(radians);
    const s = Math.sin(radians);
    const spacing = [0.3, 0.35, 0.4];
    const columns = [[1, 0, 0], [0, c, s], [0, -s, c]];
    const affine = [
        [0, 0, 0, 11.5],
        [0, 0, 0, -22.25],
        [0, 0, 0, 5],
        [0, 0, 0, 1],
    ];
    for (let col = 0; col < 3; col += 1) {
        for (let row = 0; row < 3; row += 1) {
            affine[row][col] = columns[col][row] * spacing[col];
        }
    }
    return { affine, spacing, dims: [24, 20, 18] };
})();

function tiltedHeader() {
    return {
        qform_code: 0,
        sform_code: 1,
        dims: [3, ...TILTED.dims],
        pixDims: [1, ...TILTED.spacing],
        affine: TILTED.affine,
    };
}

/** A leg that is exactly right, for the passing baseline. */
function faithfulLeg(header) {
    const described = describeGeometry(header);
    return {
        name: 'faithful',
        indexToWorldRas: (ijk) => indexToWorldRas(described.affine, ijk),
        spacing: described.spacing,
        direction: described.direction,
        axcodes: described.axcodes,
        handedness: described.handedness,
    };
}

test('comparePositions passes an exact candidate and reports zero deviation', () => {
    const header = tiltedHeader();
    const samples = sampleVoxelIndices(TILTED.dims, { count: 500 });
    const result = comparePositions({
        samples,
        reference: (ijk) => indexToWorldRas(TILTED.affine, ijk),
        candidate: (ijk) => indexToWorldRas(TILTED.affine, ijk),
    });
    assert.equal(result.passed, true);
    assert.equal(result.failures, 0);
    assert.equal(result.maxDeviationMm, 0);
    assert.equal(result.samples, samples.length);
    assert.equal(describeGeometry(header).axcodes, 'RAS');
});

test('comparePositions catches a half-voxel offset -- the fraction-convention slip', () => {
    const samples = sampleVoxelIndices(TILTED.dims, { count: 500 });
    const result = comparePositions({
        samples,
        reference: (ijk) => indexToWorldRas(TILTED.affine, ijk),
        // Corner-based fractions instead of centre-based: a uniform half-voxel shift.
        candidate: (ijk) => indexToWorldRas(TILTED.affine, ijk.map((value) => value + 0.5)),
    });
    assert.equal(result.passed, false);
    assert.equal(result.failures, samples.length, 'a uniform shift must fail every sample');
    assert.ok(result.maxDeviationMm > POSITION_TOLERANCE_MM * 1000);
    assert.ok(result.worst, 'the worst sample must be named so it can be re-examined');
});

test('comparePositions catches a mirrored candidate', () => {
    const samples = sampleVoxelIndices(TILTED.dims, { count: 300 });
    const result = comparePositions({
        samples,
        reference: (ijk) => indexToWorldRas(TILTED.affine, ijk),
        candidate: (ijk) => {
            const world = indexToWorldRas(TILTED.affine, ijk);
            return [-world[0], world[1], world[2]];
        },
    });
    assert.equal(result.passed, false);
    assert.ok(result.failures > 0);
});

test('comparePositions reports a throwing leg as failed, not as skipped', () => {
    const result = comparePositions({
        samples: sampleVoxelIndices(TILTED.dims, { count: 10 }),
        reference: (ijk) => indexToWorldRas(TILTED.affine, ijk),
        candidate: () => {
            throw new Error('volume not loaded');
        },
    });
    assert.equal(result.passed, false);
    assert.match(result.error, /volume not loaded/);
});

test('compareDerivedGeometry catches a transposed direction matrix', () => {
    // An axis-permuted affine, not the tilted one: a rotation purely about x comes out
    // of `rasToLps` as a *symmetric* direction matrix, whose transpose is itself. That
    // would make this test pass vacuously -- so the asymmetry is asserted first.
    const permuted = {
        qform_code: 0,
        sform_code: 1,
        dims: [3, 12, 14, 16],
        pixDims: [1, 0.3, 0.4, 0.5],
        affine: [
            [0, 0, 0.5, 0],
            [0.3, 0, 0, 0],
            [0, 0.4, 0, 0],
            [0, 0, 0, 1],
        ],
    };
    const reference = describeGeometry(permuted);
    const transposed = [0, 1, 2].flatMap((row) =>
        [0, 1, 2].map((col) => reference.direction[col * 3 + row])
    );
    assert.notDeepEqual(
        transposed,
        Array.from(reference.direction),
        'the fixture must not be symmetric, or this test proves nothing'
    );

    const result = compareDerivedGeometry(reference, {
        spacing: reference.spacing,
        direction: transposed,
    });
    assert.equal(result.passed, false);
    assert.match(result.issues.join(' '), /transposed nine-element literal/);
});

test('compareDerivedGeometry catches wrong spacing and disagreeing axcodes', () => {
    const reference = describeGeometry(tiltedHeader());

    const wrongSpacing = compareDerivedGeometry(reference, {
        spacing: [reference.spacing[0] * 2, reference.spacing[1], reference.spacing[2]],
        direction: reference.direction,
    });
    assert.equal(wrongSpacing.passed, false);
    assert.match(wrongSpacing.issues.join(' '), /Spacing differs/);

    const wrongAxes = compareDerivedGeometry(reference, {
        spacing: reference.spacing,
        direction: reference.direction,
        axcodes: 'LAS',
        handedness: -reference.handedness,
    });
    assert.equal(wrongAxes.passed, false);
    assert.match(wrongAxes.issues.join(' '), /mirrored or rotated study/);
    assert.match(wrongAxes.issues.join(' '), /Handedness disagrees/);
});

test('compareDerivedGeometry passes the geometry the reference itself reports', () => {
    const reference = describeGeometry(tiltedHeader());
    const result = compareDerivedGeometry(reference, {
        spacing: reference.spacing,
        direction: reference.direction,
        axcodes: reference.axcodes,
        handedness: reference.handedness,
    });
    assert.deepEqual(result.issues, []);
    assert.equal(result.passed, true);
    assert.ok(result.directionDeviation < GEOMETRY_TOLERANCE);
});

test('checkAnalyticLengths measures the affine, not the viewer', () => {
    const good = checkAnalyticLengths({
        candidate: (ijk) => indexToWorldRas(TILTED.affine, ijk),
        spacing: TILTED.spacing,
        dims: TILTED.dims,
    });
    assert.equal(good.passed, true);
    assert.equal(good.checks.length, 3);
    assert.ok(good.maxErrorMm < GEOMETRY_TOLERANCE);

    // A viewer that forgot one spacing: every position is self-consistent, every
    // distance along that axis is wrong. This is the measurement-tool failure mode.
    const squashed = checkAnalyticLengths({
        candidate: (ijk) => indexToWorldRas(TILTED.affine, [ijk[0], ijk[1], ijk[2] * 0.5]),
        spacing: TILTED.spacing,
        dims: TILTED.dims,
    });
    assert.equal(squashed.passed, false);
});

test('runTier1 passes a faithful leg and names the failing one', () => {
    const header = tiltedHeader();
    const report = runTier1({
        header,
        legs: [
            faithfulLeg(header),
            {
                name: 'mirrored',
                indexToWorldRas: (ijk) => {
                    const world = indexToWorldRas(TILTED.affine, ijk);
                    return [-world[0], world[1], world[2]];
                },
                spacing: describeGeometry(header).spacing,
                direction: describeGeometry(header).direction,
            },
        ],
        sampleCount: 300,
    });

    assert.equal(report.tier, 1);
    assert.equal(report.passed, false);
    assert.deepEqual(report.blocking, ['mirrored']);
    assert.equal(report.legs[0].passed, true);
    assert.equal(report.legs[1].passed, false);
    assert.equal(report.seed, DEFAULT_SEED);
    assert.equal(report.sampleCount, 300);
});

test('runTier1 is green with a single faithful leg', () => {
    const header = tiltedHeader();
    const report = runTier1({ header, legs: [faithfulLeg(header)], sampleCount: 200 });
    assert.equal(report.passed, true);
    assert.deepEqual(report.blocking, []);
    assert.deepEqual(report.warnings, [], 'a declared, orthonormal volume has nothing to warn about');
});

test('runTier1 carries the F2 warning even when every leg agrees', () => {
    // The case a pairwise viewer-versus-viewer diff would report as simply green: both
    // stacks consume the same fabricated affine and both may be mirroring the patient.
    const fixture = undeclaredOrientationFixture();
    const header = nifti.readHeader(fixture.buffer);
    const report = runTier1({ header, legs: [faithfulLeg(header)], sampleCount: 200 });

    assert.equal(report.passed, true, 'the legs do agree');
    assert.ok(report.warnings.length > 0, 'and the report must still say why that is not enough');
    assert.match(report.warnings.join(' '), /not that the anatomy is the right way/);
    assert.match(report.warnings.join(' '), /F2/);
});

test('runTier1 refuses to compare anything against an unusable reference', () => {
    const report = runTier1({ header: { qform_code: 1, sform_code: 1, affine: null }, legs: [] });
    assert.equal(report.passed, false);
    assert.match(report.blocking.join(' '), /reference geometry is unusable/);
});

test('a Cornerstone-shaped leg reaches the same axcodes through the LPS round trip', () => {
    // The roadmap's "axcodes from both paths through the same function": the viewer
    // reports origin/direction/spacing and no affine, so the harness reassembles one.
    const header = tiltedHeader();
    const reference = describeGeometry(header);
    const lps = rasToLps(reference.affine);

    const report = runTier1({
        header,
        legs: [
            {
                name: 'cornerstone-shaped',
                // Cornerstone works in LPS; the adapter converts back to RAS.
                indexToWorldRas: (ijk) => {
                    const [i, j, k] = ijk;
                    const world = [0, 1, 2].map(
                        (axis) =>
                            lps.origin[axis] +
                            lps.direction[axis] * lps.spacing[0] * i +
                            lps.direction[3 + axis] * lps.spacing[1] * j +
                            lps.direction[6 + axis] * lps.spacing[2] * k
                    );
                    return [-world[0], -world[1], world[2]];
                },
                spacing: lps.spacing,
                direction: lps.direction,
                axcodes: reference.axcodes,
                handedness: reference.handedness,
            },
        ],
        sampleCount: 400,
    });

    assert.equal(report.passed, true, JSON.stringify(report.legs[0], null, 2));
});

// ---------------------------------------------------------------------------
// Tier 2 -- against a faithful simulation of the upstream defect
// ---------------------------------------------------------------------------

/**
 * Reproduce `modalityScaleNifti`'s array choice and its buggy rescale gate.
 *
 * Transcribed from `@cornerstonejs/nifti-volume-loader@5.8.2`
 * `dist/esm/helpers/modalityScaleNifti.js`. The point is not to test upstream -- it is
 * to give Tier 2 the *actual* cached array it will see in the browser, so that a green
 * Tier 2 here means the residual LUT copes with what the loader really produces.
 */
function simulateUpstreamScaling(header, rawVoxels) {
    let slope = header.scl_slope;
    let inter = header.scl_inter;
    if (!slope || slope === 0 || Number.isNaN(slope)) {
        slope = 1;
    }
    if (!inter || Number.isNaN(inter)) {
        inter = 0;
    }
    const hasNegativeRescale = inter < 0 || slope < 0;
    const hasFloatRescale = inter % 1 !== 0 || slope % 1 !== 0;

    let Constructor;
    switch (header.datatypeCode) {
        case 2: // UINT8
            Constructor = hasFloatRescale ? Float32Array : hasNegativeRescale ? Int16Array : Uint8Array;
            break;
        case 4: // INT16
            Constructor = hasFloatRescale ? Float32Array : Int16Array;
            break;
        case 16: // FLOAT32
            Constructor = Float32Array;
            break;
        case 512: // UINT16
            Constructor = hasFloatRescale || hasNegativeRescale ? Float32Array : Uint16Array;
            break;
        default:
            throw new Error(`unhandled datatypeCode ${header.datatypeCode}`);
    }

    const scalarData = new Constructor(rawVoxels.length);
    scalarData.set(rawVoxels);
    // The defect, verbatim: `&&` where `||` belongs.
    if (slope !== 1 && inter !== 0) {
        for (let index = 0; index < scalarData.length; index += 1) {
            scalarData[index] = scalarData[index] * slope + inter;
        }
    }
    return scalarData;
}

test('runTier2 is green on all four rescale branches, via the residual LUT', () => {
    for (const fixture of rescaleBranchFixtures([12, 12, 12])) {
        const header = nifti.readHeader(fixture.buffer);
        const cached = simulateUpstreamScaling(header, fixture.voxels);

        // The simulation must actually reproduce the branch the fixture predicts, or
        // the rest of this assertion is testing nothing.
        const untouched = cached.every((value, index) => value === fixture.voxels[index]);
        assert.equal(untouched, fixture.upstreamSkips, `${fixture.name}: branch prediction`);

        const report = runTier2({ cached, raw: fixture.voxels, header });
        assert.equal(report.passed, true, `${fixture.name}: ${JSON.stringify(report.voxels.worst)}`);
        assert.equal(report.voxels.failures, 0, fixture.name);
        assert.equal(report.distributions.passed, true, fixture.name);
    }
});

test('runTier2 notes, in words, which studies are live instances of F1', () => {
    const wrong = rescaleBranchFixtures([8, 8, 8]).filter((fixture) => fixture.upstreamWrong);
    assert.equal(wrong.length, 2, 'two of the four branches are actively wrong');

    for (const fixture of wrong) {
        const header = nifti.readHeader(fixture.buffer);
        const report = runTier2({
            cached: simulateUpstreamScaling(header, fixture.voxels),
            raw: fixture.voxels,
            header,
        });
        assert.equal(report.passed, true);
        assert.match(report.notes.join(' '), /live instance of F1/, fixture.name);
    }

    // ...and stays quiet about the ones that are fine.
    for (const fixture of rescaleBranchFixtures([8, 8, 8]).filter((f) => !f.upstreamWrong)) {
        const header = nifti.readHeader(fixture.buffer);
        const report = runTier2({
            cached: simulateUpstreamScaling(header, fixture.voxels),
            raw: fixture.voxels,
            header,
        });
        assert.deepEqual(report.notes, [], fixture.name);
    }
});

test('runTier2 fails when the LUT is applied twice -- the mistake it exists to catch', () => {
    // `(1, -1024)`: upstream skips, so the cached array is raw. A pipeline that also
    // applied the header LUT during load would hand us data already at -1024, and the
    // residual LUT would then subtract 1024 a second time.
    const fixture = rescaleBranchFixtures([10, 10, 10])[0];
    const header = nifti.readHeader(fixture.buffer);
    const doubleApplied = Float32Array.from(fixture.voxels, (value) => value - 1024);

    const report = runTier2({ cached: doubleApplied, raw: fixture.voxels, header });
    assert.equal(report.passed, false);
    assert.ok(report.voxels.failures > 0);
    assert.ok(Math.abs(report.voxels.maxDeviation - 1024) < 1e-6, 'off by exactly one intercept');
});

test('runTier2 fails a truncated volume rather than comparing what it has', () => {
    const fixture = rescaleBranchFixtures([8, 8, 8])[2];
    const header = nifti.readHeader(fixture.buffer);
    const truncated = Int16Array.from(fixture.voxels).subarray(0, fixture.voxels.length - 64);

    const report = runTier2({ cached: truncated, raw: fixture.voxels, header });
    assert.equal(report.passed, false);
    assert.match(report.voxels.error, /Voxel counts differ/);
    assert.equal(report.distributions, null);
});

test('runTier2 catches a wrong subset of voxels through the distributions', () => {
    // A contiguous corruption -- a dropped chunk, a mis-strided frame -- which a
    // voxel sample can miss and a histogram cannot.
    const fixture = rescaleBranchFixtures([16, 16, 16])[2];
    const header = nifti.readHeader(fixture.buffer);
    const cached = Int16Array.from(fixture.voxels);
    cached.fill(20000, 0, 256);

    const report = runTier2({ cached, raw: fixture.voxels, header });
    assert.equal(report.passed, false);
    assert.ok(report.distributions.issues.length > 0);
    assert.match(report.distributions.issues.join(' '), /max differs|percentile differs/);
});

test('the value tolerance scales with magnitude, as float32 error does', () => {
    assert.equal(toleranceFor(0), 1e-3);
    assert.equal(toleranceFor(40), 1e-3);
    // At 30000 one float32 ulp is already ~0.002, so a fixed 1e-3 floor would be
    // stricter than the format the loader stores in.
    assert.ok(toleranceFor(30000) > 1e-3);
    assert.equal(toleranceFor(-30000), toleranceFor(30000));
});

test('checkAnalyticLengths keeps its runs inside the volume', () => {
    // The bug this pins: a fixed 64-voxel run starting at index 1 walks off the end of
    // any axis shorter than 66, every check gets skipped, and `passed` comes back false
    // for a viewer that was in fact perfect. Silent under-measurement reads as failure
    // here, but the same class of error reads as success elsewhere.
    for (const dims of [[3, 3, 3], [24, 20, 18], [65, 66, 67], [400, 400, 300]]) {
        const spacing = [0.3, 0.4, 0.5];
        const affine = diagonalAffine(spacing);
        const result = checkAnalyticLengths({
            candidate: (ijk) => indexToWorldRas(affine, ijk),
            spacing,
            dims,
        });
        assert.equal(result.checks.length, 3, `${JSON.stringify(dims)}: every axis must be measured`);
        assert.equal(result.passed, true, JSON.stringify(dims));
        for (const check of result.checks) {
            assert.ok(check.steps >= 1, `${JSON.stringify(dims)}: a run must have length`);
        }
    }
});

test('checkAnalyticLengths reports failure, not success, when a volume is too thin', () => {
    // A single-voxel axis has no distance to measure. Better to say so than to invent
    // a zero-length run that trivially agrees.
    const spacing = [1, 1, 1];
    const result = checkAnalyticLengths({
        candidate: (ijk) => indexToWorldRas(diagonalAffine(spacing), ijk),
        spacing,
        dims: [1, 1, 1],
    });
    assert.deepEqual(result.checks, []);
    assert.equal(result.passed, false);
});
