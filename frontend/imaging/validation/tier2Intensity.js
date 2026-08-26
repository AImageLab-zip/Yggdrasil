/**
 * Tier 2 -- intensity. This is the tier that catches F1.
 *
 * The claim under test is one sentence: **after our residual LUT, the voxel values
 * Cornerstone cached are the values the NIfTI header says they are.** The header says
 * `modality = raw * scl_slope + scl_inter`, unconditionally and for every voxel, and
 * that arithmetic is not in question -- it is `nifti1.h`. What is in question is
 * whether the pipeline that produced `scalarData` performed it.
 *
 * It sometimes does. `modalityScaleNifti.js` gates the rescale on
 * `if (slope !== 1 && inter !== 0)`, where the operator must be `||`, so the loop is
 * skipped whenever *either* factor is neutral. Two of the four branches are therefore
 * left raw and two are scaled, with nothing on the volume recording which -- see
 * `imaging/metadata/modalityLutModule.js`. `residualModalityLut` is the mitigation;
 * this tier is what proves the mitigation is right on real bytes rather than on the
 * four synthetic headers the unit tests cover.
 *
 * Two things are compared, and they fail differently:
 *
 *   - **Voxel-exact agreement** over a sample. Catches a wrong LUT, a wrong branch, a
 *     truncating write-back, an endianness slip. This is the gate.
 *   - **Histogram and percentile agreement** over everything. Catches a *subset* of
 *     voxels being wrong -- a partial decompression, a dropped final chunk, a frame
 *     mis-stride -- which a sample of 10^4 out of 10^8 can easily miss entirely.
 *
 * Pure: it takes two arrays and a header. Reading the raw bytes independently is the
 * adapter's job.
 */

import { applyModalityLut, normalizeScaling, residualModalityLut, upstreamAppliesRescale } from '../metadata/modalityLutModule.js';
import { computeHistogram, percentileValues, robustRange } from '../windowing/autoVoi.js';
import { DEFAULT_SEED, mulberry32 } from './prng.js';

/**
 * Absolute floor for value agreement, in modality units.
 *
 * Not zero: `modalityScaleNifti` may write into a `Float32Array`, and a value needing
 * more than 24 bits of mantissa comes back slightly different from the same arithmetic
 * done in a double. 1e-3 HU is far below any clinically meaningful difference.
 */
export const VALUE_TOLERANCE = 1e-3;

/**
 * Relative component of the tolerance -- roughly float32's precision.
 *
 * An absolute floor alone would be wrong in both directions. Float32 error scales with
 * magnitude, so 1e-3 HU is generous at 40 HU and *stricter than the format* at 30000,
 * where one ulp is already ~0.002. A fixed floor would therefore fail honest volumes
 * with large values while passing nothing extra at small ones.
 */
export const VALUE_RELATIVE_TOLERANCE = 1e-6;

/**
 * The tolerance that applies at a given magnitude.
 *
 * @param {number} expected
 * @returns {number}
 */
export function toleranceFor(expected) {
    return Math.max(VALUE_TOLERANCE, Math.abs(expected) * VALUE_RELATIVE_TOLERANCE);
}

/** Percentiles reported side by side, beyond the robust pair. */
export const REPORTED_PERCENTILES = Object.freeze([0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]);

/** How many voxels the exact comparison samples, when not comparing all of them. */
export const DEFAULT_VOXEL_SAMPLES = 200000;

/**
 * Compare cached scalar data against the header's own arithmetic, voxel by voxel.
 *
 * @param {object} options
 * @param {ArrayLike<number>} options.cached what Cornerstone put in the volume.
 * @param {ArrayLike<number>} options.raw the stored values, read independently.
 * @param {object} options.header the parsed NIfTI header.
 * @param {number} [options.sampleCount]
 * @param {number} [options.seed]
 * @returns {object}
 */
export function compareVoxelValues({
    cached,
    raw,
    header,
    sampleCount = DEFAULT_VOXEL_SAMPLES,
    seed = DEFAULT_SEED,
}) {
    if (cached.length !== raw.length) {
        return {
            passed: false,
            samples: 0,
            maxDeviation: NaN,
            worst: null,
            failures: 1,
            error:
                `Voxel counts differ: cached ${cached.length}, raw ${raw.length}. ` +
                'A truncated fetch or a dropped final chunk looks like this.',
        };
    }

    const headerLut = normalizeScaling(header);
    const residual = residualModalityLut(header);
    const random = mulberry32(seed);
    const total = cached.length;
    const step = total > sampleCount ? total / sampleCount : 1;

    let maxDeviation = 0;
    let failures = 0;
    let samples = 0;
    let worst = null;

    // Stratified rather than uniform-random: a volume is laid out slice by slice, and
    // the failures worth finding (a dropped chunk, a mis-strided frame) are contiguous.
    // A stratified walk hits every region; a uniform draw can leave a whole slice unseen.
    for (let position = 0; position < total; position += step) {
        const index = Math.min(total - 1, Math.floor(position + random() * step));
        // The definition, applied unconditionally -- no branch, because the branch is
        // the bug.
        const expected = applyModalityLut(raw[index], headerLut);
        const actual = applyModalityLut(cached[index], residual);
        const deviation = Math.abs(actual - expected);

        samples += 1;
        if (!Number.isFinite(deviation) || deviation > toleranceFor(expected)) {
            failures += 1;
        }
        if (!(deviation <= maxDeviation)) {
            maxDeviation = deviation;
            worst = { index, raw: raw[index], cached: cached[index], expected, actual, deviation };
        }
    }

    return {
        passed: failures === 0 && samples > 0,
        samples,
        maxDeviation,
        worst,
        failures,
        error: null,
        headerLut,
        residualLut: residual,
        upstreamApplied: upstreamAppliesRescale(header),
    };
}

/**
 * Compare the full distributions, not a sample of them.
 *
 * @param {object} options as {@link compareVoxelValues}, minus the sampling.
 * @returns {object}
 */
export function compareDistributions({ cached, raw, header }) {
    const headerLut = normalizeScaling(header);
    const residual = residualModalityLut(header);

    const expected = describeDistribution(raw, headerLut);
    const actual = describeDistribution(cached, residual);

    const issues = [];
    const deviations = {};
    for (const key of ['min', 'max', 'robustMin', 'robustMax']) {
        const deviation = Math.abs(expected[key] - actual[key]);
        deviations[key] = deviation;
        if (!(deviation <= toleranceFor(expected[key]))) {
            issues.push(`${key} differs by ${deviation.toExponential(3)} (${expected[key]} vs ${actual[key]}).`);
        }
    }

    const percentileDeviations = REPORTED_PERCENTILES.map((cut, slot) => {
        const deviation = Math.abs(expected.percentiles[slot] - actual.percentiles[slot]);
        if (!(deviation <= toleranceFor(expected.percentiles[slot]))) {
            issues.push(
                `The ${(cut * 100).toFixed(0)}th percentile differs by ` +
                    `${deviation.toExponential(3)} (${expected.percentiles[slot]} vs ${actual.percentiles[slot]}).`
            );
        }
        return { percentile: cut, expected: expected.percentiles[slot], actual: actual.percentiles[slot], deviation };
    });

    return { passed: issues.length === 0, issues, expected, actual, deviations, percentileDeviations };
}

/**
 * Summarise one array under a LUT: range, robust range and a percentile ladder.
 *
 * @param {ArrayLike<number>} data
 * @param {{rescaleSlope: number, rescaleIntercept: number}} lut
 * @returns {object}
 */
export function describeDistribution(data, lut) {
    // One binning pass, then arithmetic on 256 numbers. Percentile-by-percentile
    // traversal would be eight passes over 10^8 voxels for a single report.
    const binned = computeHistogram(data);
    const { min, max, robustMin, robustMax } = robustRange(binned);
    return {
        count: binned.count,
        skipped: binned.skipped,
        min: applyModalityLut(min, lut),
        max: applyModalityLut(max, lut),
        robustMin: applyModalityLut(robustMin, lut),
        robustMax: applyModalityLut(robustMax, lut),
        percentiles: percentileValues(binned, REPORTED_PERCENTILES).map((value) =>
            applyModalityLut(value, lut)
        ),
    };
}

/**
 * Run the whole of Tier 2 for one study.
 *
 * @param {object} options
 * @param {ArrayLike<number>} options.cached
 * @param {ArrayLike<number>} options.raw
 * @param {object} options.header
 * @param {number} [options.sampleCount]
 * @param {number} [options.seed]
 * @returns {object}
 */
export function runTier2({ cached, raw, header, sampleCount, seed = DEFAULT_SEED }) {
    const voxels = compareVoxelValues({ cached, raw, header, sampleCount, seed });
    const distributions = voxels.error ? null : compareDistributions({ cached, raw, header });

    const { rescaleSlope, rescaleIntercept } = normalizeScaling(header);
    const notes = [];
    if (!upstreamAppliesRescale(header) && !(rescaleSlope === 1 && rescaleIntercept === 0)) {
        notes.push(
            `The loader skipped this volume's rescale (slope ${rescaleSlope}, ` +
                `intercept ${rescaleIntercept}); every cached voxel is a raw stored ` +
                'value. This study is a live instance of F1 -- the residual LUT is ' +
                'what makes the numbers below agree.'
        );
    }

    return {
        tier: 2,
        passed: voxels.passed && Boolean(distributions?.passed),
        seed,
        voxels,
        distributions,
        notes,
        blocking: [
            ...(voxels.passed ? [] : ['voxel-exact agreement']),
            ...(distributions && !distributions.passed ? ['distribution agreement'] : []),
        ],
    };
}
