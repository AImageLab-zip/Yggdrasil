/**
 * Windowing, in the two units it has to exist in at once.
 *
 * Decision #5 says a window is a real modality range -- a CT bone window is
 * -450..1050 HU, on every scanner, written down once. But Cornerstone's
 * `setProperties({ voiRange })` clips against **the values in the cached scalar
 * array**, and finding F1 means that array is in raw stored units for two of the four
 * rescale shapes and in modality units for the other two, with nothing on the volume
 * recording which.
 *
 * So a preset cannot be handed to a viewport as written. It has to be pushed through
 * the *residual* LUT first, and the direction matters: `toStoredValue` going in,
 * `applyModalityLut` coming back out for anything shown to a person. Everything in
 * this module is one of those two conversions with a name on it, because the failure
 * mode of getting one backwards is a window that looks plausible and is 1024 HU wrong.
 *
 * Pure -- no Cornerstone import. The viewport call site is one line in the shell.
 */

import {
    applyModalityLut,
    residualModalityLut,
    toStoredValue,
} from '../metadata/modalityLutModule.js';
import { autoVoi, hasAbsolutePresets, presetVoi } from '../windowing/autoVoi.js';

/**
 * Convert a modality-unit window into the stored-unit range a viewport clips against.
 *
 * @param {{windowCenter: number, windowWidth: number}} voi in modality units.
 * @param {{rescaleSlope: number, rescaleIntercept: number}} residual from
 *   {@link residualModalityLut} -- **not** the header's own LUT.
 * @returns {{lower: number, upper: number}} in the units `scalarData` holds.
 */
export function voiRangeFromModalityWindow({ windowCenter, windowWidth }, residual) {
    const half = windowWidth / 2;
    const lower = toStoredValue(windowCenter - half, residual);
    const upper = toStoredValue(windowCenter + half, residual);
    // A negative slope inverts the interval. Legal, rare, and it would otherwise hand
    // the renderer a range whose lower bound exceeds its upper.
    return lower <= upper ? { lower, upper } : { lower: upper, upper: lower };
}

/**
 * Convert a viewport's stored-unit range back into modality units, for display.
 *
 * The inverse of {@link voiRangeFromModalityWindow}. This is what the window/level
 * readout shows and what a saved preset would record -- a number in stored units means
 * nothing to anyone and cannot be compared between studies.
 *
 * @param {{lower: number, upper: number}} range in stored units.
 * @param {{rescaleSlope: number, rescaleIntercept: number}} residual
 * @returns {{windowCenter: number, windowWidth: number}} in modality units.
 */
export function modalityWindowFromVoiRange({ lower, upper }, residual) {
    const low = applyModalityLut(lower, residual);
    const high = applyModalityLut(upper, residual);
    const [min, max] = low <= high ? [low, high] : [high, low];
    return { windowCenter: (min + max) / 2, windowWidth: max - min };
}

/**
 * The VOI a volume should open on, ready to hand to a viewport.
 *
 * Per decision #16: an absolute preset where the modality has one, the volume's own
 * robust percentiles where it does not. CBCT and MRI always take the second path --
 * CBCT greyscale is not calibrated Hounsfield, so a fixed preset there would be a
 * number that looks authoritative and is not.
 *
 * @param {object} options
 * @param {ArrayLike<number>} options.scalarData the volume's cached data.
 * @param {object} options.header the parsed NIfTI header.
 * @param {string} [options.modality] e.g. `'ct'`, `'cbct'`, `'mri'`.
 * @param {string} [options.preset] a preset name, when the caller wants a specific one.
 * @returns {{
 *   range: {lower: number, upper: number},
 *   window: {windowCenter: number, windowWidth: number},
 *   residual: {rescaleSlope: number, rescaleIntercept: number},
 *   source: string, label: string|null
 * }}
 */
export function openingVoi({ scalarData, header, modality, preset }) {
    const residual = residualModalityLut(header);

    if (preset) {
        const named = presetVoi(modality, preset);
        if (!named) {
            throw new Error(
                `No preset '${preset}' for modality '${modality}'. ` +
                    (hasAbsolutePresets(modality)
                        ? 'Check the preset name.'
                        : `'${modality}' has no absolute presets -- its greyscale is not a ` +
                          'calibrated unit, so its window is derived from the data (decision #16).')
            );
        }
        return {
            range: voiRangeFromModalityWindow(named, residual),
            window: { windowCenter: named.windowCenter, windowWidth: named.windowWidth },
            residual,
            source: 'preset',
            label: named.label,
        };
    }

    const derived = autoVoi(scalarData, { header });
    return {
        range: voiRangeFromModalityWindow(derived, residual),
        window: { windowCenter: derived.windowCenter, windowWidth: derived.windowWidth },
        residual,
        source: 'auto',
        label: null,
    };
}

/**
 * Format a window for the on-screen readout.
 *
 * Always in modality units, and **always with the unit named**. An unlabelled "300 /
 * 1500" is exactly the ambiguity decision #5 exists to remove: the old sliders showed
 * two numbers that meant percent-of-this-volume's-range and read like Hounsfield.
 *
 * @param {{windowCenter: number, windowWidth: number}} window
 * @param {string} [unit] `'HU'` for calibrated CT, otherwise the empty string.
 * @returns {string}
 */
export function formatWindow({ windowCenter, windowWidth }, unit = '') {
    const suffix = unit ? ` ${unit}` : '';
    return `W ${Math.round(windowWidth)}${suffix} / L ${Math.round(windowCenter)}${suffix}`;
}

/**
 * The unit a modality's values are in, or the empty string when there is not one.
 *
 * CBCT deliberately returns `''`. Its greyscale is vendor-dependent and not Hounsfield,
 * and labelling it "HU" would dress a relative number up as a physical measurement --
 * the same mistake as reporting an uncalibrated pixel length in millimetres, which
 * `annotations` enforces against with a `CHECK` constraint.
 *
 * @param {string} modality
 * @returns {string}
 */
export function unitFor(modality) {
    return String(modality).toLowerCase() === 'ct' ? 'HU' : '';
}
