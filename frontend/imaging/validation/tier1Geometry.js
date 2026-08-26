/**
 * Tier 1 -- geometry. Exact, and it blocks deletion.
 *
 * The question this tier answers is narrow and load-bearing: **if we delete NiiVue and
 * render this study through Cornerstone, does the anatomy land in the same place?**
 * With no feature flags (decision #3), there is no runtime comparison to fall back on,
 * so this pre-merge check is the whole safety net.
 *
 * The design point worth arguing for: this is a **three-legged** comparison, not a
 * diff between the two viewers.
 *
 *   - The **reference** leg is the file's own affine, put through
 *     `imaging/geometry/orientation.js`. That is not a third opinion; it is the
 *     definition. NIfTI says voxel `(i, j, k)` is at `A * (i, j, k, 1)` in RAS, and
 *     both viewers are implementations of that sentence.
 *   - The **Cornerstone** leg is `indexToWorld` on the loaded volume.
 *   - The **NiiVue** leg is `frac2mm`, after the index-space translation in
 *     `voxelSampling.js`.
 *
 * A pairwise diff would report "they agree" when both are wrong the same way -- which
 * is exactly what would happen for the F2 population, where both stacks consume the
 * same fabricated affine and both render the same possibly-mirrored volume. Against
 * the reference leg, that case shows up as *agreement plus a declared-orientation
 * warning*, which is the truth: the two viewers match, and neither of them knows which
 * way round the patient is.
 *
 * Everything in this module is pure. It takes callables, not viewers -- the adapters
 * in `adapters.js` are what touch NiiVue and Cornerstone, and they are the only part
 * that cannot be exercised under `node --test`.
 */

import { describeGeometry, indexToWorldRas, orthonormalityDefect } from '../geometry/orientation.js';
import { DEFAULT_SAMPLE_COUNT, sampleVoxelIndices } from './voxelSampling.js';
import { DEFAULT_SEED } from './prng.js';

/** Roadmap Tier 1: `max|Δ| < 1e-4 mm` between paths. */
export const POSITION_TOLERANCE_MM = 1e-4;

/** Roadmap Tier 1: direction cosines orthonormal, and spacing equal, to 1e-6. */
export const GEOMETRY_TOLERANCE = 1e-6;

/**
 * Relative tolerance for a length, against the magnitude of the coordinates it spans.
 *
 * The roadmap gives one figure, 1e-6, for "dims, spacing, direction cosines" and for
 * the analytic length check. That is right for the first three -- direction cosines are
 * dimensionless and near 1, spacings are small -- and **unsatisfiable for the fourth**
 * on a stack that computes in single precision.
 *
 * gl-matrix, which NiiVue uses for `frac2mm`, sets `ARRAY_TYPE = Float32Array`. One
 * float32 ulp at a patient coordinate of 130 mm is 1.5e-5 mm, and a length is the
 * difference of two such positions. Demanding agreement to 1e-6 mm therefore demands
 * better than the format can represent: the first real harness run failed the NiiVue
 * length check on 21 of 31 maxillo studies with position deviations of 1.0e-5 to
 * 2.9e-5 mm -- exactly one to two ulps -- while Cornerstone, which computes in
 * float64, passed every one at 1e-6.
 *
 * So the tolerance scales with the coordinate magnitude. 1e-6 relative is about eight
 * float32 ulps, which leaves room for a few accumulated operations, and at a realistic
 * 130 mm it works out to 1.3e-4 mm -- still four thousand times smaller than the finest
 * CBCT voxel, so a real spacing error cannot hide inside it.
 *
 * The absolute {@link GEOMETRY_TOLERANCE} remains the floor, so a volume near the
 * origin is still held to it.
 */
export const COORDINATE_RELATIVE_TOLERANCE = 1e-6;

/**
 * The length tolerance that applies at a given coordinate magnitude.
 *
 * @param {number} magnitudeMm the largest absolute ordinate involved.
 * @returns {number} millimetres.
 */
export function lengthToleranceFor(magnitudeMm) {
    return Math.max(GEOMETRY_TOLERANCE, Math.abs(magnitudeMm) * COORDINATE_RELATIVE_TOLERANCE);
}

/**
 * Compare one leg's world positions against the reference, over a sample.
 *
 * @param {object} options
 * @param {number[][]} options.samples voxel indices, in file storage order.
 * @param {(ijk: number[]) => number[]} options.reference RAS mm, from the affine.
 * @param {(ijk: number[]) => number[]} options.candidate RAS mm, from a viewer.
 * @param {number} [options.tolerance] mm.
 * @returns {{
 *   samples: number, maxDeviationMm: number, meanDeviationMm: number,
 *   worst: {index: number[], reference: number[], candidate: number[], deviationMm: number}|null,
 *   failures: number, passed: boolean, error: string|null
 * }}
 */
export function comparePositions({ samples, reference, candidate, tolerance = POSITION_TOLERANCE_MM }) {
    let maxDeviationMm = 0;
    let total = 0;
    let failures = 0;
    let worst = null;

    for (const index of samples) {
        let expected;
        let actual;
        try {
            expected = reference(index);
            actual = candidate(index);
        } catch (error) {
            // One throwing sample invalidates the leg, not just the sample: a viewer
            // that cannot map a voxel it owns has a different problem from a viewer
            // that maps it to the wrong place, and silently skipping would hide it.
            return {
                samples: 0,
                maxDeviationMm: NaN,
                meanDeviationMm: NaN,
                worst: null,
                failures: samples.length,
                passed: false,
                error: `mapping voxel ${JSON.stringify(index)} threw: ${error.message}`,
            };
        }

        const deviationMm = Math.hypot(
            actual[0] - expected[0],
            actual[1] - expected[1],
            actual[2] - expected[2]
        );
        if (!Number.isFinite(deviationMm)) {
            failures += 1;
            continue;
        }
        total += deviationMm;
        if (deviationMm > maxDeviationMm) {
            maxDeviationMm = deviationMm;
            worst = { index, reference: expected, candidate: actual, deviationMm };
        }
        if (deviationMm >= tolerance) {
            failures += 1;
        }
    }

    return {
        samples: samples.length,
        maxDeviationMm,
        meanDeviationMm: samples.length ? total / samples.length : NaN,
        worst,
        failures,
        passed: failures === 0 && samples.length > 0,
        error: null,
    };
}

/**
 * Compare the derived geometry -- spacing, direction, axcodes, handedness.
 *
 * Separate from {@link comparePositions} because it fails differently: a spacing that
 * is wrong by a factor is visible in every measurement, while a direction that is
 * transposed still produces plausible positions for a symmetric volume.
 *
 * @param {object} reference from `describeGeometry`.
 * @param {object} candidate `{spacing, direction, axcodes?}` reported by a viewer.
 * @param {number} [tolerance]
 * @returns {{passed: boolean, issues: string[], spacingDeviation: number, directionDeviation: number}}
 */
export function compareDerivedGeometry(reference, candidate, tolerance = GEOMETRY_TOLERANCE) {
    const issues = [];

    const spacingDeviation = Math.max(
        ...[0, 1, 2].map((axis) => Math.abs(reference.spacing[axis] - candidate.spacing[axis]))
    );
    if (!(spacingDeviation < tolerance)) {
        issues.push(
            `Spacing differs by ${spacingDeviation.toExponential(3)} mm ` +
                `(${JSON.stringify(reference.spacing)} vs ${JSON.stringify(candidate.spacing)}).`
        );
    }

    const directionDeviation = Math.max(
        ...[...Array(9).keys()].map((slot) =>
            Math.abs(reference.direction[slot] - candidate.direction[slot])
        )
    );
    if (!(directionDeviation < tolerance)) {
        issues.push(
            `Direction cosines differ by ${directionDeviation.toExponential(3)}. ` +
                'A transposed nine-element literal looks exactly like this.'
        );
    }

    const candidateDefect = orthonormalityDefect(candidate.direction);
    if (!(candidateDefect < tolerance)) {
        issues.push(
            `Reported direction cosines are not orthonormal ` +
                `(worst deviation ${candidateDefect.toExponential(3)}).`
        );
    }

    // Axcodes through the *same* function on both sides -- the roadmap's requirement.
    // The candidate has no affine of its own, so it is reassembled from the geometry
    // it does report, which re-exercises the RAS/LPS transposition on the way.
    if (candidate.axcodes !== undefined && candidate.axcodes !== reference.axcodes) {
        issues.push(
            `Anatomical axes disagree: reference says ${reference.axcodes}, ` +
                `viewer says ${candidate.axcodes}. This is a mirrored or rotated study.`
        );
    }

    if (candidate.handedness !== undefined && candidate.handedness !== reference.handedness) {
        issues.push(
            `Handedness disagrees (${reference.handedness} vs ${candidate.handedness}): ` +
                'the volume is mirrored.'
        );
    }

    return { passed: issues.length === 0, issues, spacingDeviation, directionDeviation };
}

/**
 * The analytic length check: two voxel centres, a distance computable from the affine.
 *
 * Independent of both viewers on purpose. A stack can get every *position* right
 * relative to its own origin and still report the wrong *distance* if it has applied a
 * spacing twice or not at all, and distance is what a measurement tool reports -- the
 * feature this whole migration exists to add.
 *
 * @param {object} options
 * @param {(ijk: number[]) => number[]} options.candidate world mm, any frame.
 * @param {number[]} options.spacing per-axis mm, from the reference affine.
 * @param {number[]} options.dims voxel counts.
 * @param {number} [options.tolerance] mm.
 * @returns {{passed: boolean, checks: object[], maxErrorMm: number}}
 */
export function checkAnalyticLengths({ candidate, spacing, dims, tolerance = null }) {
    const checks = [];
    let maxErrorMm = 0;

    // One voxel in from the corner, so a viewer that clamps at the boundary is not
    // measured against its own clamp.
    const from = [1, 1, 1].map((value, axis) => (dims[axis] > 2 ? value : 0));

    for (let axis = 0; axis < 3; axis += 1) {
        // A long run rather than one voxel: a per-step rounding error accumulates and
        // becomes visible, while a single step can hide inside the tolerance. The run
        // has to end inside the volume -- `dims[axis] - 1` is the last valid index, and
        // the start offset comes out of the budget.
        const steps = Math.min(dims[axis] - 1 - from[axis], 64);
        if (steps < 1) {
            continue;
        }
        const to = from.slice();
        to[axis] += steps;

        const a = candidate(from);
        const b = candidate(to);
        const measuredMm = Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]);
        const expectedMm = steps * spacing[axis];
        const errorMm = Math.abs(measuredMm - expectedMm);

        // Scaled to the coordinates actually involved, not to the length: the error is
        // inherited from the two endpoint positions, and on a float32 stack those carry
        // an ulp proportional to their distance from the origin, not to their
        // separation. See COORDINATE_RELATIVE_TOLERANCE.
        const magnitudeMm = Math.max(...a.map(Math.abs), ...b.map(Math.abs));
        const applied = tolerance ?? lengthToleranceFor(magnitudeMm);

        maxErrorMm = Math.max(maxErrorMm, errorMm);
        checks.push({
            axis,
            steps,
            measuredMm,
            expectedMm,
            errorMm,
            toleranceMm: applied,
            magnitudeMm,
            passed: errorMm < applied,
        });
    }

    return {
        passed: checks.length > 0 && checks.every((check) => check.passed),
        checks,
        maxErrorMm,
        toleranceMm: checks.length ? Math.max(...checks.map((check) => check.toleranceMm)) : NaN,
    };
}

/**
 * Run the whole of Tier 1 for one study.
 *
 * @param {object} options
 * @param {object} options.header the parsed NIfTI header (the reference).
 * @param {object[]} options.legs viewer legs, each
 *   `{name, indexToWorldRas, spacing, direction, axcodes?, handedness?}`.
 * @param {number} [options.sampleCount]
 * @param {number} [options.seed]
 * @returns {object} a structured Tier 1 report.
 */
export function runTier1({ header, legs, sampleCount = DEFAULT_SAMPLE_COUNT, seed = DEFAULT_SEED }) {
    const reference = describeGeometry(header);
    const dims = reference.dimensions;

    if (!Number.isFinite(reference.determinant) || dims.some((size) => size < 1)) {
        return {
            tier: 1,
            passed: false,
            reference,
            seed,
            legs: [],
            blocking: ['The reference geometry is unusable; nothing can be compared against it.'],
        };
    }

    const samples = sampleVoxelIndices(dims, { count: sampleCount, seed });
    const referenceIndexToWorld = (ijk) => indexToWorldRas(reference.affine, ijk);

    const results = legs.map((leg) => {
        const positions = comparePositions({
            samples,
            reference: referenceIndexToWorld,
            candidate: leg.indexToWorldRas,
        });
        const derived = compareDerivedGeometry(reference, {
            spacing: leg.spacing,
            direction: leg.direction,
            axcodes: leg.axcodes,
            handedness: leg.handedness,
        });
        const lengths = checkAnalyticLengths({
            candidate: leg.indexToWorldRas,
            spacing: reference.spacing,
            dims,
        });
        return {
            name: leg.name,
            positions,
            derived,
            lengths,
            passed: positions.passed && derived.passed && lengths.passed,
        };
    });

    // The F2 caveat travels with the result rather than being logged and lost: when
    // the file declares no orientation, the legs can agree perfectly and the study can
    // still be mirrored. That is a fact about the data, not a failure of the migration,
    // so it is a warning and not a gate -- but it must appear in the report.
    const warnings = reference.declared
        ? []
        : [
              'This study declares no orientation (qform_code = sform_code = 0). Both ' +
                  'viewers consume the same fabricated affine, so agreement here says ' +
                  'the migration is faithful -- not that the anatomy is the right way ' +
                  'round. See finding F2.',
          ];

    return {
        tier: 1,
        passed: results.every((result) => result.passed),
        reference,
        seed,
        sampleCount: samples.length,
        legs: results,
        warnings: [...warnings, ...reference.issues],
        blocking: results.filter((result) => !result.passed).map((result) => result.name),
    };
}
