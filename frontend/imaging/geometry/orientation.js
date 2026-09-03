/**
 * NIfTI affine geometry: the one place voxel indices become patient millimetres.
 *
 * Pure by construction -- no Cornerstone import, no DOM, no fetch -- because this is
 * the module the Phase 3 validation harness runs on *both* sides of its comparison.
 * Tier 1 of docs/cornerstone-roadmap.md requires "axcodes from both paths through the
 * same function"; that is only meaningful if the function has no hidden dependency on
 * which stack produced its input. So everything here takes plain numbers.
 *
 * Three conventions collide in this file, and mixing them is the failure mode the
 * whole harness exists to catch:
 *
 *   - **NIfTI world space is RAS.** `affine[row][col]` maps `(i, j, k, 1)` to
 *     `(x, y, z)` with +x right, +y anterior, +z superior.
 *   - **DICOM and Cornerstone patient space is LPS.** +x left, +y posterior. The two
 *     differ by negating x and y -- two sign flips, which is exactly a mirroring
 *     across the sagittal and coronal planes if applied once too few or once too many.
 *     Phase 2 made LPS and RAS separate `coordinate_system` values in the durable
 *     model for this reason (`annotations/constants.py`); this module is the runtime
 *     half of that decision.
 *   - **Row-major versus column-major.** `parseAffineMatrix` upstream returns its
 *     `orientation` **row-major**, and `rasToLps` then re-emits it **column-major**
 *     (three consecutive direction cosines, one per voxel axis) because that is what
 *     Cornerstone's `direction` wants. The transposition is silent, it is buried in a
 *     nine-element array literal, and getting it wrong produces a volume that looks
 *     plausible and is rotated. {@link rasToLps} below reproduces it deliberately so
 *     the harness can diff against an independent derivation
 *     ({@link directionFromRasAffine}) rather than against itself.
 *
 * Finding F2 of the roadmap lives here too: `static/js/nifti-reader.js:701-704`
 * fabricates a diagonal affine from `pixDims` when `qform_code < 1 && sform_code < 1`,
 * with positive signs -- i.e. it *assumes* RAS storage order. `rasToLps` then converts
 * that fiction into a confident-looking LPS direction and Cornerstone renders it
 * without complaint. {@link declaresOrientation} is how a caller finds out, and
 * {@link fallbackAffineFromPixDims} reproduces the fabrication so a test can assert on
 * it instead of hoping.
 */

/** Anatomical letter pairs, indexed by RAS axis: 0 = x, 1 = y, 2 = z. */
export const AXIS_PAIRS = Object.freeze([
    Object.freeze(['L', 'R']),
    Object.freeze(['P', 'A']),
    Object.freeze(['I', 'S']),
]);

/** Tolerance for "these direction cosines are orthonormal" (roadmap Tier 1). */
export const ORTHONORMALITY_TOLERANCE = 1e-6;

function isFiniteNumber(value) {
    return typeof value === 'number' && Number.isFinite(value);
}

/**
 * Determinant of the affine's 3x3 rotation/scale block.
 *
 * Its *sign* is the volume's handedness: negative means the storage order is
 * left-handed relative to RAS. A handedness flip between two paths is a mirrored
 * study, so the harness reports this rather than only comparing magnitudes.
 *
 * @param {number[][]} affine 4x4, `affine[row][col]`.
 * @returns {number}
 */
export function affineDeterminant(affine) {
    const [a, b, c] = [affine[0][0], affine[0][1], affine[0][2]];
    const [d, e, f] = [affine[1][0], affine[1][1], affine[1][2]];
    const [g, h, i] = [affine[2][0], affine[2][1], affine[2][2]];
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g);
}

/**
 * True when the affine is fully finite and non-degenerate.
 *
 * Deliberately identical in behaviour to `VolumeMetadata.isAffineValid`
 * (`static/js/volume_metadata.js`), which the legacy CBCT conversion worker still
 * depends on. `frontend/tests/orientation.test.js` pins the two implementations to
 * each other over a shared corpus so the ESM rewrite cannot drift from the UMD one
 * while both are alive.
 *
 * @param {number[][]} affine
 * @returns {boolean}
 */
export function isAffineValid(affine) {
    if (!affine || affine.length < 4) {
        return false;
    }
    for (let row = 0; row < 3; row += 1) {
        const line = affine[row];
        if (!line || line.length < 4) {
            return false;
        }
        for (let col = 0; col < 4; col += 1) {
            if (!isFiniteNumber(line[col])) {
                return false;
            }
        }
    }
    const determinant = affineDeterminant(affine);
    return isFiniteNumber(determinant) && Math.abs(determinant) >= 1e-9;
}

/**
 * Derive the nibabel-style axcodes string (e.g. `"RAS"`, `"LPI"`) from a RAS affine.
 *
 * Each *column* of the 3x3 block maps one storage axis into physical space; the
 * dominant component picks the anatomical axis and its sign picks the direction.
 *
 * @param {number[][]} affine 4x4 RAS affine.
 * @returns {string|null} three letters, or null when the mapping is degenerate.
 */
export function affineToOrientation(affine) {
    if (!isAffineValid(affine)) {
        return null;
    }
    const codes = [];
    for (let col = 0; col < 3; col += 1) {
        const values = [affine[0][col], affine[1][col], affine[2][col]];
        let best = 0;
        for (let row = 1; row < 3; row += 1) {
            if (Math.abs(values[row]) > Math.abs(values[best])) {
                best = row;
            }
        }
        if (values[best] === 0) {
            return null;
        }
        codes.push(values[best] > 0 ? AXIS_PAIRS[best][1] : AXIS_PAIRS[best][0]);
    }
    return codes.join('');
}

/**
 * Split a RAS affine into origin, row-major orientation and spacing.
 *
 * A faithful re-implementation of `parseAffineMatrix` in
 * `@cornerstonejs/nifti-volume-loader@5.8.2`
 * (`dist/esm/helpers/affineUtilities.js`), row-major output included. It exists so
 * the harness can assert our understanding of upstream *is* upstream, and so
 * {@link rasToLps} can be built on the same footing the real loader uses.
 *
 * @param {number[][]} affine 4x4 RAS affine.
 * @returns {{origin: number[], orientation: number[], spacing: number[]}}
 */
export function parseAffineMatrix(affine) {
    const origin = [affine[0][3], affine[1][3], affine[2][3]];
    const spacing = [0, 1, 2].map((col) =>
        Math.sqrt(affine[0][col] ** 2 + affine[1][col] ** 2 + affine[2][col] ** 2)
    );
    const orientation = [];
    for (let row = 0; row < 3; row += 1) {
        for (let col = 0; col < 3; col += 1) {
            orientation.push(affine[row][col] / spacing[col]);
        }
    }
    return { origin, orientation, spacing };
}

/**
 * Convert a RAS affine to the LPS origin/direction/spacing Cornerstone consumes.
 *
 * Reproduces `rasToLps` from the shipped loader exactly, transposition and all: the
 * returned `direction` is **column-major** -- `direction.slice(0, 3)` is the LPS unit
 * vector of voxel axis i, `slice(3, 6)` of j, `slice(6, 9)` of k.
 *
 * @param {number[][]} affine 4x4 RAS affine.
 * @returns {{origin: number[], direction: number[], spacing: number[]}}
 */
export function rasToLps(affine) {
    const { origin, orientation, spacing } = parseAffineMatrix(affine);
    return {
        origin: [-origin[0], -origin[1], origin[2]],
        // Upstream writes this out as a flat literal; the indices are the transpose.
        direction: [
            -orientation[0], -orientation[3], orientation[6],
            -orientation[1], -orientation[4], orientation[7],
            -orientation[2], -orientation[5], orientation[8],
        ],
        spacing,
    };
}

/**
 * Derive the same LPS direction *without* going through the row-major detour.
 *
 * Independent on purpose. {@link rasToLps} mirrors upstream's index gymnastics; this
 * one reads each affine column directly, normalises it, and negates x and y. If the
 * two disagree, upstream's literal is transposed and every volume is rotated -- which
 * is not a hypothesis the harness should have to take on trust.
 *
 * @param {number[][]} affine 4x4 RAS affine.
 * @returns {{origin: number[], direction: number[], spacing: number[]}}
 */
export function directionFromRasAffine(affine) {
    const spacing = [];
    const direction = [];
    for (let col = 0; col < 3; col += 1) {
        const column = [affine[0][col], affine[1][col], affine[2][col]];
        const norm = Math.hypot(column[0], column[1], column[2]);
        spacing.push(norm);
        direction.push(-column[0] / norm, -column[1] / norm, column[2] / norm);
    }
    return {
        origin: [-affine[0][3], -affine[1][3], affine[2][3]],
        direction,
        spacing,
    };
}

/**
 * Rebuild a RAS affine from Cornerstone's LPS geometry.
 *
 * The inverse of {@link rasToLps}, and the reason Tier 1 can put *both* stacks through
 * {@link affineToOrientation}: the Cornerstone side has no affine of its own, only an
 * origin, a column-major direction and a spacing, so it has to be reassembled before
 * the shared axcode function can see it. Round-tripping through here also re-exercises
 * the transposition, which is where a mirroring bug would hide.
 *
 * @param {object} geometry
 * @param {number[]} geometry.origin LPS, 3 elements.
 * @param {number[]} geometry.direction LPS, column-major, 9 elements.
 * @param {number[]} geometry.spacing 3 elements.
 * @returns {number[][]} 4x4 RAS affine.
 */
export function affineFromLpsGeometry({ origin, direction, spacing }) {
    const affine = [
        [0, 0, 0, -origin[0]],
        [0, 0, 0, -origin[1]],
        [0, 0, 0, origin[2]],
        [0, 0, 0, 1],
    ];
    for (let col = 0; col < 3; col += 1) {
        const lps = direction.slice(col * 3, col * 3 + 3);
        affine[0][col] = -lps[0] * spacing[col];
        affine[1][col] = -lps[1] * spacing[col];
        affine[2][col] = lps[2] * spacing[col];
    }
    return affine;
}

/**
 * Map a voxel index to RAS millimetres.
 *
 * Continuous, not rounded: the harness feeds it fractional indices as well as integer
 * voxel centres, because an off-by-half-a-voxel origin convention only shows up
 * between samples.
 *
 * @param {number[][]} affine 4x4 RAS affine.
 * @param {number[]} ijk voxel index, 3 elements.
 * @returns {number[]} `[x, y, z]` in RAS mm.
 */
export function indexToWorldRas(affine, ijk) {
    const [i, j, k] = ijk;
    return [0, 1, 2].map(
        (row) =>
            affine[row][0] * i + affine[row][1] * j + affine[row][2] * k + affine[row][3]
    );
}

/**
 * Map a voxel index to LPS millimetres -- the frame Cornerstone's `indexToWorld` uses.
 *
 * @param {number[][]} affine 4x4 RAS affine.
 * @param {number[]} ijk voxel index, 3 elements.
 * @returns {number[]} `[x, y, z]` in LPS mm.
 */
export function indexToWorldLps(affine, ijk) {
    const [x, y, z] = indexToWorldRas(affine, ijk);
    return [-x, -y, z];
}

/**
 * How far a column-major direction matrix is from orthonormal.
 *
 * Returns the worst absolute deviation over the three unit-length checks and the
 * three pairwise-orthogonality checks, so one number can be compared against
 * {@link ORTHONORMALITY_TOLERANCE}. Reported rather than thrown: a slightly
 * non-orthonormal affine is common in real studies and the harness's job is to say
 * how bad it is, not to decide.
 *
 * @param {number[]} direction 9 elements, column-major.
 * @returns {number}
 */
export function orthonormalityDefect(direction) {
    const columns = [0, 1, 2].map((col) => direction.slice(col * 3, col * 3 + 3));
    let worst = 0;
    for (let a = 0; a < 3; a += 1) {
        for (let b = a; b < 3; b += 1) {
            const dot =
                columns[a][0] * columns[b][0] +
                columns[a][1] * columns[b][1] +
                columns[a][2] * columns[b][2];
            const expected = a === b ? 1 : 0;
            worst = Math.max(worst, Math.abs(dot - expected));
        }
    }
    return worst;
}

/**
 * Whether the header declares an orientation at all.
 *
 * The F2 gate. `qform_code` and `sform_code` are both below 1 for the population
 * `annotations_normalize_coordinates` counted in Phase 2 -- volumes whose orientation
 * is *inferred from pixel dimensions* and therefore may be mirrored. Callers must
 * surface this to the user; they must not silently render it as though it were known.
 *
 * @param {object} header parsed NIfTI header (`nifti-reader-js` shape).
 * @returns {boolean}
 */
export function declaresOrientation(header) {
    const qform = Number(header?.qform_code) | 0;
    const sform = Number(header?.sform_code) | 0;
    return qform >= 1 || sform >= 1;
}

/**
 * The diagonal affine `nifti-reader-js` fabricates when no orientation is declared.
 *
 * Reproduced here so a test can assert what the fiction actually is rather than
 * describing it in prose. Note the positive signs: the fabrication assumes RAS
 * storage order, and nothing downstream ever revisits that assumption.
 *
 * @param {number[]} pixDims NIfTI `pixDims`, 1-indexed for the spatial axes.
 * @returns {number[][]} 4x4 RAS affine.
 */
export function fallbackAffineFromPixDims(pixDims) {
    const spacing = [1, 2, 3].map((index) => {
        const value = Number(pixDims?.[index]);
        return Number.isFinite(value) && value > 0 ? value : 1;
    });
    return [
        [spacing[0], 0, 0, 0],
        [0, spacing[1], 0, 0],
        [0, 0, spacing[2], 0],
        [0, 0, 0, 1],
    ];
}

/**
 * Everything the geometry side of the harness reports about one volume.
 *
 * A single call so the two paths cannot accidentally be described with different
 * fields, and so `hasMetadata: false` is impossible to forget: it is part of the
 * return value, not a separate question the caller may or may not ask.
 *
 * @param {object} header parsed NIfTI header.
 * @returns {{
 *   affine: number[][], declared: boolean, hasMetadata: boolean,
 *   axcodes: string|null, determinant: number, handedness: number,
 *   origin: number[], direction: number[], spacing: number[],
 *   dimensions: number[], orthonormalityDefect: number, issues: string[]
 * }}
 */
export function describeGeometry(header) {
    const declared = declaresOrientation(header);
    const issues = [];

    let affine = header?.affine ?? null;
    if (!declared) {
        issues.push(
            'qform_code and sform_code are both 0: the orientation is inferred from ' +
                'pixel dimensions and may be mirrored (finding F2).'
        );
        // Use whatever the reader produced, but only after saying so above. Falling
        // back to our own reconstruction here would hide a difference between the
        // reader's fabrication and ours, which is precisely what Tier 1 must see.
        if (!isAffineValid(affine)) {
            affine = fallbackAffineFromPixDims(header?.pixDims);
        }
    }

    if (!isAffineValid(affine)) {
        issues.push('The affine is degenerate or unreadable.');
        return {
            affine: affine ?? null,
            declared,
            hasMetadata: false,
            axcodes: null,
            determinant: NaN,
            handedness: 0,
            origin: [NaN, NaN, NaN],
            direction: new Array(9).fill(NaN),
            spacing: [NaN, NaN, NaN],
            dimensions: dimensionsOf(header),
            orthonormalityDefect: NaN,
            issues,
        };
    }

    const axcodes = affineToOrientation(affine);
    if (axcodes === null) {
        issues.push('The affine could not be mapped to anatomical axes.');
    }

    const { origin, direction, spacing } = rasToLps(affine);
    const determinant = affineDeterminant(affine);
    const defect = orthonormalityDefect(direction);
    if (defect > ORTHONORMALITY_TOLERANCE) {
        issues.push(
            `Direction cosines are not orthonormal to ${ORTHONORMALITY_TOLERANCE} ` +
                `(worst deviation ${defect.toExponential(3)}).`
        );
    }

    return {
        affine,
        declared,
        hasMetadata: declared && axcodes !== null,
        axcodes,
        determinant,
        handedness: Math.sign(determinant),
        origin,
        direction,
        spacing,
        dimensions: dimensionsOf(header),
        orthonormalityDefect: defect,
        issues,
    };
}

function dimensionsOf(header) {
    const dims = header?.dims;
    if (!Array.isArray(dims) && !ArrayBuffer.isView(dims)) {
        return [0, 0, 0];
    }
    return [1, 2, 3].map((index) => Number(dims[index]) | 0);
}
