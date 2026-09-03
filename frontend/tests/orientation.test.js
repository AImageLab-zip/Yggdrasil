import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

import {
    AXIS_PAIRS,
    ORTHONORMALITY_TOLERANCE,
    affineDeterminant,
    affineFromLpsGeometry,
    affineToOrientation,
    declaresOrientation,
    describeGeometry,
    directionFromRasAffine,
    fallbackAffineFromPixDims,
    indexToWorldLps,
    indexToWorldRas,
    isAffineValid,
    orthonormalityDefect,
    parseAffineMatrix,
    rasToLps,
} from '../imaging/geometry/orientation.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..', '..');

/**
 * The shipped loader's own conversion, loaded from node_modules rather than
 * paraphrased, so the cross-checks below compare against the version pinned in
 * `package-lock.json` and not against the version someone read the docs for.
 *
 * By file URL, not by package specifier: `@cornerstonejs/nifti-volume-loader` has an
 * `exports` map that does not publish `./dist/esm/helpers/*`, and node enforces it.
 * The path is deliberately explicit -- if a future bump moves these helpers, this
 * import fails loudly rather than the cross-checks quietly comparing us to ourselves.
 */
const loaderHelpers = join(REPO, 'node_modules', '@cornerstonejs', 'nifti-volume-loader', 'dist', 'esm', 'helpers');
const { rasToLps: upstreamRasToLps } = await import(pathToFileURL(join(loaderHelpers, 'convert.js')));
const { parseAffineMatrix: upstreamParseAffineMatrix } = await import(
    pathToFileURL(join(loaderHelpers, 'affineUtilities.js'))
);

// ---------------------------------------------------------------------------
// A corpus every cross-check runs over.
// ---------------------------------------------------------------------------

/** Build a RAS affine from per-axis direction columns, spacings and an origin. */
function affineOf(columns, spacing, origin) {
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

/** A rotation about the x axis, so the corpus is not all axis-aligned. */
function rotatedColumns(radians) {
    const c = Math.cos(radians);
    const s = Math.sin(radians);
    return [
        [1, 0, 0],
        [0, c, s],
        [0, -s, c],
    ];
}

const CORPUS = [
    {
        name: 'identity, isotropic -- RAS',
        affine: affineOf([[1, 0, 0], [0, 1, 0], [0, 0, 1]], [1, 1, 1], [0, 0, 0]),
        axcodes: 'RAS',
    },
    {
        name: 'anisotropic CBCT, LAS storage order',
        affine: affineOf([[-1, 0, 0], [0, 1, 0], [0, 0, 1]], [0.3, 0.3, 0.4], [96.2, -104.5, -31.7]),
        axcodes: 'LAS',
    },
    {
        name: 'radiological LPI',
        affine: affineOf([[-1, 0, 0], [0, -1, 0], [0, 0, -1]], [0.5, 0.5, 1], [120, 120, 60]),
        axcodes: 'LPI',
    },
    {
        name: 'axis-permuted: storage k is superior-inferior reversed',
        affine: affineOf([[0, 1, 0], [0, 0, 1], [1, 0, 0]], [1, 1, 2], [-10, -20, -30]),
        axcodes: 'ASR',
    },
    {
        name: 'obliquely rotated 12 degrees about x',
        affine: affineOf(rotatedColumns((12 * Math.PI) / 180), [0.4, 0.4, 0.4], [3, -2, 1]),
        axcodes: 'RAS',
    },
    {
        name: 'brain MRI, 1mm, RPI',
        affine: affineOf([[1, 0, 0], [0, -1, 0], [0, 0, -1]], [1, 1, 1], [-90, 126, 72]),
        axcodes: 'RPI',
    },
];

// ---------------------------------------------------------------------------
// Axcodes
// ---------------------------------------------------------------------------

test('affineToOrientation names the anatomical axes of every corpus affine', () => {
    for (const study of CORPUS) {
        assert.equal(affineToOrientation(study.affine), study.axcodes, study.name);
    }
});

test('AXIS_PAIRS covers each anatomical axis in the low-to-high direction', () => {
    assert.deepEqual(
        AXIS_PAIRS.map((pair) => pair.join('')),
        ['LR', 'PA', 'IS']
    );
});

test('affineToOrientation refuses a degenerate affine rather than guessing', () => {
    const singular = affineOf([[1, 0, 0], [1, 0, 0], [0, 0, 1]], [1, 1, 1], [0, 0, 0]);
    assert.equal(isAffineValid(singular), false);
    assert.equal(affineToOrientation(singular), null);
    assert.equal(affineToOrientation(null), null);
    assert.equal(affineToOrientation([[1, 0, 0, NaN], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]), null);
});

/**
 * The ESM rewrite must not drift from the UMD module the legacy CBCT conversion worker
 * still loads (`static/js/worker/cbct_convert_worker.js:496,531,601`). Finding F2 says
 * `volume_metadata.js` is kept and re-wired, not deleted, so for as long as both are
 * alive they have to agree -- otherwise a volume converts under one orientation rule
 * and renders under another.
 */
test('the ESM geometry agrees with the UMD VolumeMetadata it will co-exist with', () => {
    const source = readFileSync(join(REPO, 'static', 'js', 'volume_metadata.js'), 'utf8');
    const sandbox = { module: { exports: {} }, window: undefined };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox);
    const legacy = sandbox.module.exports;

    assert.ok(legacy.affineToOrientation, 'volume_metadata.js must have exported its API');

    for (const study of CORPUS) {
        assert.equal(
            affineToOrientation(study.affine),
            legacy.affineToOrientation(study.affine),
            `axcodes disagree for ${study.name}`
        );
        assert.equal(
            isAffineValid(study.affine),
            legacy.isAffineValid(study.affine),
            `validity disagrees for ${study.name}`
        );
    }

    // And on the shapes that decide the F2 branch.
    const degenerate = [
        null,
        [[0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 1]],
        [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, Infinity, 0], [0, 0, 0, 1]],
    ];
    for (const affine of degenerate) {
        assert.equal(affineToOrientation(affine), legacy.affineToOrientation(affine));
        assert.equal(isAffineValid(affine), legacy.isAffineValid(affine));
    }
});

// ---------------------------------------------------------------------------
// RAS -> LPS, against the shipped loader
// ---------------------------------------------------------------------------

test('parseAffineMatrix reproduces the shipped loader, row-major output included', () => {
    for (const study of CORPUS) {
        const ours = parseAffineMatrix(study.affine);
        const theirs = upstreamParseAffineMatrix(study.affine);
        assert.deepEqual(ours.origin, theirs.origin, study.name);
        assert.deepEqual(ours.spacing, theirs.spacing, study.name);
        assert.deepEqual(ours.orientation, theirs.orientation, study.name);
    }
});

test('rasToLps reproduces the shipped loader exactly', () => {
    for (const study of CORPUS) {
        const ours = rasToLps(study.affine);
        const theirs = upstreamRasToLps({ affine: study.affine });
        assert.deepEqual(ours.origin, theirs.origin, study.name);
        assert.deepEqual(ours.spacing, theirs.spacing, study.name);
        // Upstream calls it `orientation`; it is Cornerstone's `direction`.
        assert.deepEqual(ours.direction, theirs.orientation, study.name);
    }
});

/**
 * The transposition check. `rasToLps` mirrors upstream's flat nine-element literal;
 * `directionFromRasAffine` reads each affine column directly. If the literal's indices
 * were transposed, every volume would render rotated and nothing would raise -- so the
 * two derivations are computed independently and compared.
 */
test('the column-major direction is a transpose-free derivation of the same thing', () => {
    for (const study of CORPUS) {
        const viaUpstreamShape = rasToLps(study.affine);
        const direct = directionFromRasAffine(study.affine);
        assert.deepEqual(direct.origin, viaUpstreamShape.origin, study.name);
        for (let index = 0; index < 9; index += 1) {
            assert.ok(
                Math.abs(direct.direction[index] - viaUpstreamShape.direction[index]) < 1e-12,
                `direction[${index}] disagrees for ${study.name}`
            );
        }
    }
});

test('direction slices are the per-voxel-axis unit vectors, in order i, j, k', () => {
    // A deliberately un-permuted, un-rotated case, so the expected answer is readable.
    const { direction } = rasToLps(
        affineOf([[1, 0, 0], [0, 1, 0], [0, 0, 1]], [0.3, 0.4, 0.5], [0, 0, 0])
    );
    assert.deepEqual(direction.slice(0, 3), [-1, -0, 0]); // i: RAS +x -> LPS -x
    assert.deepEqual(direction.slice(3, 6), [-0, -1, 0]); // j: RAS +y -> LPS -y
    assert.deepEqual(direction.slice(6, 9), [-0, -0, 1]); // k: RAS +z -> LPS +z
});

test('affineFromLpsGeometry inverts rasToLps, so both paths can share one axcode fn', () => {
    for (const study of CORPUS) {
        const geometry = rasToLps(study.affine);
        const rebuilt = affineFromLpsGeometry(geometry);
        for (let row = 0; row < 4; row += 1) {
            for (let col = 0; col < 4; col += 1) {
                assert.ok(
                    Math.abs(rebuilt[row][col] - study.affine[row][col]) < 1e-9,
                    `[${row}][${col}] differs for ${study.name}`
                );
            }
        }
        // The point of the round trip: the Cornerstone side reaches the same axcodes.
        assert.equal(affineToOrientation(rebuilt), study.axcodes, study.name);
    }
});

// ---------------------------------------------------------------------------
// index -> world
// ---------------------------------------------------------------------------

test('indexToWorldLps is indexToWorldRas with x and y negated', () => {
    const { affine } = CORPUS[1];
    for (const ijk of [[0, 0, 0], [1, 2, 3], [10.5, -4, 77.25]]) {
        const ras = indexToWorldRas(affine, ijk);
        const lps = indexToWorldLps(affine, ijk);
        assert.deepEqual(lps, [-ras[0], -ras[1], ras[2]]);
    }
});

test('voxel (0,0,0) lands on the origin, in both frames', () => {
    for (const study of CORPUS) {
        const { origin } = rasToLps(study.affine);
        assert.deepEqual(indexToWorldLps(study.affine, [0, 0, 0]), origin, study.name);
        assert.deepEqual(indexToWorldRas(study.affine, [0, 0, 0]), [
            study.affine[0][3],
            study.affine[1][3],
            study.affine[2][3],
        ]);
    }
});

/**
 * The roadmap's Tier 1 "analytic length check": the distance between two voxel centres
 * separated along one storage axis is that axis's spacing times the index difference,
 * whatever the rotation. Checked to 1e-6 mm, and in LPS -- the frame the measurement
 * tools will actually report in.
 */
test('distance between voxel centres matches the affine analytically, to 1e-6 mm', () => {
    for (const study of CORPUS) {
        const { spacing } = rasToLps(study.affine);
        for (let axis = 0; axis < 3; axis += 1) {
            const steps = 17;
            const a = [3, 5, 7];
            const b = a.slice();
            b[axis] += steps;
            const pa = indexToWorldLps(study.affine, a);
            const pb = indexToWorldLps(study.affine, b);
            const measured = Math.hypot(pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]);
            assert.ok(
                Math.abs(measured - steps * spacing[axis]) < 1e-6,
                `${study.name}: axis ${axis} measured ${measured}, expected ${steps * spacing[axis]}`
            );
        }
    }
});

// ---------------------------------------------------------------------------
// Orthonormality and handedness
// ---------------------------------------------------------------------------

test('orthonormality defect is ~0 for well-formed affines and grows when sheared', () => {
    for (const study of CORPUS) {
        const { direction } = rasToLps(study.affine);
        assert.ok(
            orthonormalityDefect(direction) < ORTHONORMALITY_TOLERANCE,
            `${study.name} should be orthonormal`
        );
    }

    const sheared = affineOf([[1, 0, 0], [0.05, 1, 0], [0, 0, 1]], [1, 1, 1], [0, 0, 0]);
    assert.ok(orthonormalityDefect(rasToLps(sheared).direction) > 1e-3);
});

/**
 * Chirality. Pure affine arithmetic cannot catch a mirroring introduced downstream,
 * but it can catch one introduced *here*: the determinant's sign is the handedness,
 * and negating a single axis must flip it.
 */
test('handedness follows the determinant sign and flips on a single-axis mirror', () => {
    const right = affineOf([[1, 0, 0], [0, 1, 0], [0, 0, 1]], [1, 1, 1], [0, 0, 0]);
    const mirrored = affineOf([[-1, 0, 0], [0, 1, 0], [0, 0, 1]], [1, 1, 1], [0, 0, 0]);

    assert.ok(affineDeterminant(right) > 0);
    assert.ok(affineDeterminant(mirrored) < 0);
    assert.equal(describeGeometry({ sform_code: 1, affine: right }).handedness, 1);
    assert.equal(describeGeometry({ sform_code: 1, affine: mirrored }).handedness, -1);

    // And the axcodes change with it, which is what a reviewer would actually see.
    assert.equal(affineToOrientation(right), 'RAS');
    assert.equal(affineToOrientation(mirrored), 'LAS');
});

// ---------------------------------------------------------------------------
// F2: the undeclared-orientation population
// ---------------------------------------------------------------------------

test('declaresOrientation is false only when both codes are below 1', () => {
    assert.equal(declaresOrientation({ qform_code: 0, sform_code: 0 }), false);
    assert.equal(declaresOrientation({ qform_code: 1, sform_code: 0 }), true);
    assert.equal(declaresOrientation({ qform_code: 0, sform_code: 2 }), true);
    assert.equal(declaresOrientation({}), false);
});

test('fallbackAffineFromPixDims reproduces the reader fiction, positive signs and all', () => {
    // static/js/nifti-reader.js:701-704 assigns pixDims[1..3] to the diagonal, which
    // asserts RAS storage order without any evidence for it.
    const affine = fallbackAffineFromPixDims([1, 0.3, 0.3, 0.4]);
    assert.deepEqual(affine, [
        [0.3, 0, 0, 0],
        [0, 0.3, 0, 0],
        [0, 0, 0.4, 0],
        [0, 0, 0, 1],
    ]);
    assert.equal(affineToOrientation(affine), 'RAS');

    // Missing or nonsensical spacings become 1 rather than 0, which would be singular.
    assert.deepEqual(fallbackAffineFromPixDims([1, 0, -2, NaN]), [
        [1, 0, 0, 0],
        [0, 1, 0, 0],
        [0, 0, 1, 0],
        [0, 0, 0, 1],
    ]);
});

test('describeGeometry reports hasMetadata false and says why, for the F2 population', () => {
    const header = {
        qform_code: 0,
        sform_code: 0,
        pixDims: [1, 0.3, 0.3, 0.4],
        dims: [3, 400, 400, 300],
        affine: fallbackAffineFromPixDims([1, 0.3, 0.3, 0.4]),
    };
    const described = describeGeometry(header);

    assert.equal(described.declared, false);
    assert.equal(described.hasMetadata, false);
    // The axcodes are still computed -- they are what the viewer would use -- but the
    // caller has been told they are inferred.
    assert.equal(described.axcodes, 'RAS');
    assert.deepEqual(described.dimensions, [400, 400, 300]);
    assert.match(described.issues.join(' '), /inferred from pixel dimensions/);
    assert.match(described.issues.join(' '), /F2/);
});

test('describeGeometry reports hasMetadata true for a declared, sane volume', () => {
    const described = describeGeometry({
        qform_code: 0,
        sform_code: 1,
        dims: [3, 512, 512, 400],
        pixDims: [1, 0.3, 0.3, 0.3],
        affine: CORPUS[1].affine,
    });
    assert.equal(described.declared, true);
    assert.equal(described.hasMetadata, true);
    assert.equal(described.axcodes, 'LAS');
    assert.deepEqual(described.issues, []);
    assert.deepEqual(described.dimensions, [512, 512, 400]);
});

test('describeGeometry degrades without throwing on an unreadable affine', () => {
    const described = describeGeometry({ qform_code: 1, sform_code: 1, affine: null });
    assert.equal(described.hasMetadata, false);
    assert.equal(described.axcodes, null);
    assert.match(described.issues.join(' '), /degenerate or unreadable/);
});
