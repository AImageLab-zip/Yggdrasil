/**
 * Windowing in real modality values. Percent-of-data-range does not exist here.
 *
 * Decision #5 of docs/cornerstone-roadmap.md is a clean break: the sliders at
 * `templates/maxillo/patient_detail_content.html:75-77` run `min=1 max=100` and mean
 * "percent of this volume's own data range", which is not a clinical quantity. Two
 * scans of the same patient on the same machine get different percentages for the same
 * bone, and no preset can be written down. Everything below is in the volume's own
 * modality units -- Hounsfield for CT/CBCT, arbitrary-but-stable for MRI.
 *
 * Decision #16 splits the problem in two, and the split is the whole design:
 *
 *   - **Absolute presets where the unit means something.** A CT bone window is
 *     300/1500 HU on every scanner ever built. {@link MODALITY_PRESETS} states those,
 *     and they are constants, not derived from the data.
 *   - **Robust auto-VOI where it does not.** CBCT vendors do not agree on a HU scale
 *     and MRI has no absolute unit at all, so the opening window is derived from the
 *     volume's own robust percentiles. {@link autoVoi} does that.
 *
 * The percentile machinery is a port of `volumeRange` from
 * `static/js/modality_viewers/niivue_render_modes.js:136-166`, which Phase 3 deletes.
 * Its *fallback chain* is the valuable part and is reproduced exactly -- see
 * {@link volumeRange}. What could not be ported is where its inputs came from:
 * `robust_min`/`robust_max` were computed by NiiVue itself, inside a library that is
 * going away, so {@link robustRange} computes them here instead.
 */

import { applyModalityLut, residualModalityLut } from '../metadata/modalityLutModule.js';

/**
 * Percentile cuts for the robust range.
 *
 * 2% and 98% match what NiiVue's `calMinMax` used, so the opening window on an already
 * -uploaded study does not visibly jump when Phase 3 replaces the viewer under it.
 * That continuity is the reason for these particular numbers; there is nothing
 * clinically special about them.
 */
export const DEFAULT_ROBUST_PERCENTILES = Object.freeze({ low: 0.02, high: 0.98 });

/**
 * Histogram resolution for {@link robustRange}.
 *
 * A histogram rather than a sort: a CBCT volume is 10^8 voxels and sorting it to find
 * two percentiles would allocate a second copy and take seconds on the main thread.
 * 256 bins is also what NiiVue used, so the ported numbers stay comparable.
 */
export const HISTOGRAM_BINS = 256;

/** VOI floor. A zero-width window is a divide-by-zero in every renderer. */
export const MINIMUM_WINDOW_WIDTH = 1e-6;

function finiteNumber(value, fallback) {
    return Number.isFinite(value) ? value : fallback;
}

function clamp(value, low, high) {
    return Math.min(Math.max(value, low), high);
}

/**
 * Exact minimum and maximum of the finite samples, plus how many were not finite.
 *
 * NaN and +/-Infinity are skipped rather than propagated. They occur in real MRI
 * derivatives (a masked-out background written as NaN), and one of them poisons every
 * comparison it touches -- a single NaN would otherwise make min and max NaN and the
 * whole VOI undefined.
 *
 * @param {ArrayLike<number>} scalarData
 * @returns {{min: number, max: number, count: number, skipped: number}}
 */
export function scalarRange(scalarData) {
    let min = Infinity;
    let max = -Infinity;
    let skipped = 0;
    const length = scalarData.length;
    for (let index = 0; index < length; index += 1) {
        const value = scalarData[index];
        if (!Number.isFinite(value)) {
            skipped += 1;
            continue;
        }
        if (value < min) {
            min = value;
        }
        if (value > max) {
            max = value;
        }
    }
    if (min > max) {
        // Every sample was non-finite, or the array was empty.
        return { min: NaN, max: NaN, count: 0, skipped };
    }
    return { min, max, count: length - skipped, skipped };
}

/**
 * Robust minimum and maximum: the values at the low and high percentile cuts.
 *
 * Single pass to bin, second pass over the 256 bins to accumulate -- so the cost is
 * one traversal of the data regardless of volume size. The returned values are bin
 * *edges*, which means they are quantised to `(max - min) / bins`; that is accurate
 * enough for an opening window and is exactly what the NiiVue implementation this
 * replaces also did.
 *
 * @param {ArrayLike<number>} scalarData
 * @param {object} [options]
 * @param {number} [options.low] low cut, 0..1.
 * @param {number} [options.high] high cut, 0..1.
 * @param {number} [options.bins]
 * @returns {{min: number, max: number, robustMin: number, robustMax: number}}
 */
export function robustRange(scalarData, options = {}) {
    const low = finiteNumber(options.low, DEFAULT_ROBUST_PERCENTILES.low);
    const high = finiteNumber(options.high, DEFAULT_ROBUST_PERCENTILES.high);
    const bins = Math.max(2, Math.floor(finiteNumber(options.bins, HISTOGRAM_BINS)));

    const { min, max, count } = scalarRange(scalarData);
    if (!Number.isFinite(min) || !Number.isFinite(max)) {
        return { min: NaN, max: NaN, robustMin: NaN, robustMax: NaN };
    }
    if (min === max || count === 0) {
        // A constant volume has no percentiles worth the name.
        return { min, max, robustMin: min, robustMax: max };
    }

    const histogram = new Float64Array(bins);
    const scale = bins / (max - min);
    const length = scalarData.length;
    for (let index = 0; index < length; index += 1) {
        const value = scalarData[index];
        if (!Number.isFinite(value)) {
            continue;
        }
        // `max` itself lands one bin past the end; clamp rather than widen the range,
        // which would shift every other bin edge.
        const bin = Math.min(bins - 1, Math.floor((value - min) * scale));
        histogram[bin] += 1;
    }

    const lowTarget = count * clamp(low, 0, 1);
    const highTarget = count * clamp(high, 0, 1);
    let cumulative = 0;
    let robustMinBin = 0;
    let robustMaxBin = bins - 1;
    let foundLow = false;
    for (let bin = 0; bin < bins; bin += 1) {
        cumulative += histogram[bin];
        if (!foundLow && cumulative >= lowTarget) {
            robustMinBin = bin;
            foundLow = true;
        }
        if (cumulative >= highTarget) {
            robustMaxBin = bin;
            break;
        }
    }

    const binWidth = (max - min) / bins;
    return {
        min,
        max,
        robustMin: min + robustMinBin * binWidth,
        // The upper cut is the *far* edge of its bin, or the robust range would be
        // systematically narrow by one bin at the top.
        robustMax: min + (robustMaxBin + 1) * binWidth,
    };
}

/**
 * Reconcile the four range sources into one usable range.
 *
 * A direct port of `volumeRange` in `niivue_render_modes.js:136-166`, minus its
 * conversion to percent. The fallback chain is the part worth keeping and is
 * reproduced decision for decision:
 *
 *   1. Prefer the global (true) range; fall back to the header's `cal_min`/`cal_max`.
 *   2. If the result is not a real interval, fall back again and force a width of at
 *      least one unit -- `cal_min == cal_max` is common in headers written by tools
 *      that never filled the field in.
 *   3. Clamp the robust range inside the global range, and if *that* collapses, widen
 *      it back out to the global range rather than returning an empty window.
 *
 * Step 3 is the one that matters in practice: a volume that is 99% air has a robust
 * range narrower than one histogram bin, and without the widening the viewer opens on
 * a window in which the anatomy is entirely clipped.
 *
 * @param {object} sources
 * @param {number} [sources.min] global minimum.
 * @param {number} [sources.max] global maximum.
 * @param {number} [sources.calMin] header `cal_min`.
 * @param {number} [sources.calMax] header `cal_max`.
 * @param {number} [sources.robustMin]
 * @param {number} [sources.robustMax]
 * @returns {{min: number, max: number, robustMin: number, robustMax: number}}
 */
export function volumeRange(sources = {}) {
    const fallbackMin = finiteNumber(sources.calMin, 0);
    const fallbackMax = finiteNumber(sources.calMax, fallbackMin + 1);
    let min = finiteNumber(sources.min, fallbackMin);
    let max = finiteNumber(sources.max, fallbackMax);
    if (!(max > min)) {
        min = fallbackMin;
        max = fallbackMax > fallbackMin ? fallbackMax : fallbackMin + 1;
    }

    let robustMin = clamp(finiteNumber(sources.robustMin, min), min, max);
    let robustMax = clamp(finiteNumber(sources.robustMax, max), min, max);
    if (!(robustMax > robustMin)) {
        robustMin = min;
        robustMax = max;
    }

    return { min, max, robustMin, robustMax };
}

/**
 * Turn a range into a Cornerstone VOI.
 *
 * @param {{robustMin: number, robustMax: number}} range
 * @returns {{windowCenter: number, windowWidth: number}} in modality units.
 */
export function voiFromRange({ robustMin, robustMax }) {
    return {
        windowCenter: (robustMin + robustMax) / 2,
        windowWidth: Math.max(MINIMUM_WINDOW_WIDTH, robustMax - robustMin),
    };
}

/** A VOI expressed as the two values it clips at, which is what a preset reads like. */
export function voiFromLimits(lower, upper) {
    return voiFromRange({ robustMin: lower, robustMax: upper });
}

/**
 * Named windows in absolute modality units.
 *
 * Only for modalities where the unit is defined: real CT. Every value is a
 * conventional radiological window written as `[lower, upper]` in HU, not as
 * centre/width, because that is how they are read off a scanner console and it makes
 * the interval visible.
 *
 * CBCT is deliberately absent. Its greyscale is not calibrated Hounsfield -- the same
 * anatomy reads differently between vendors and between fields of view on one machine
 * -- so an absolute preset there would be a number that looks authoritative and is
 * not. CBCT gets {@link autoVoi} instead. Same for MRI, which has no absolute unit.
 */
export const MODALITY_PRESETS = Object.freeze({
    ct: Object.freeze({
        bone: Object.freeze({ label: 'Bone', lower: -450, upper: 1050 }),
        softTissue: Object.freeze({ label: 'Soft tissue', lower: -160, upper: 240 }),
        brain: Object.freeze({ label: 'Brain', lower: 0, upper: 80 }),
        lung: Object.freeze({ label: 'Lung', lower: -1350, upper: 150 }),
        airway: Object.freeze({ label: 'Airway', lower: -1000, upper: -400 }),
    }),
});

/**
 * Whether absolute presets are meaningful for a modality (decision #16).
 *
 * @param {string} modality lowercase modality key, e.g. `'ct'`, `'cbct'`, `'mri'`.
 * @returns {boolean}
 */
export function hasAbsolutePresets(modality) {
    return Object.prototype.hasOwnProperty.call(MODALITY_PRESETS, String(modality).toLowerCase());
}

/**
 * Look up one named preset as a VOI.
 *
 * @param {string} modality
 * @param {string} preset
 * @returns {{windowCenter: number, windowWidth: number, label: string}|null}
 */
export function presetVoi(modality, preset) {
    const table = MODALITY_PRESETS[String(modality).toLowerCase()];
    const entry = table?.[preset];
    if (!entry) {
        return null;
    }
    return { ...voiFromLimits(entry.lower, entry.upper), label: entry.label };
}

/**
 * Derive the opening VOI for one volume, in modality units.
 *
 * Applies the *residual* LUT, not the header's: after the loader has run, `scalarData`
 * is in raw units for two of the four rescale shapes and in modality units for the
 * other two (finding F1), and only `residualModalityLut` knows which. Deriving a
 * window from the wrong one is how a CBCT opens 1024 HU off with everything still
 * looking like an image.
 *
 * @param {ArrayLike<number>} scalarData the volume's cached scalar data.
 * @param {object} options
 * @param {object} options.header the parsed NIfTI header.
 * @param {object} [options.percentiles] forwarded to {@link robustRange}.
 * @returns {{
 *   windowCenter: number, windowWidth: number,
 *   range: {min: number, max: number, robustMin: number, robustMax: number},
 *   lut: {rescaleSlope: number, rescaleIntercept: number}
 * }}
 */
export function autoVoi(scalarData, { header, percentiles } = {}) {
    const lut = residualModalityLut(header);
    const raw = robustRange(scalarData, percentiles);

    // Affine, so the percentile *positions* are unaffected by the LUT and only the
    // values need mapping. That is why the histogram runs on raw data: binning after
    // a per-voxel multiply-add would cost a second full traversal for the same answer.
    const range = volumeRange({
        min: applyModalityLut(raw.min, lut),
        max: applyModalityLut(raw.max, lut),
        calMin: applyModalityLut(finiteNumber(header?.cal_min, NaN), lut),
        calMax: applyModalityLut(finiteNumber(header?.cal_max, NaN), lut),
        robustMin: applyModalityLut(raw.robustMin, lut),
        robustMax: applyModalityLut(raw.robustMax, lut),
    });

    return { ...voiFromRange(range), range, lut };
}
