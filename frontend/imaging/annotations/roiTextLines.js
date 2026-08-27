/**
 * What an ROI annotation says on screen: the area, and nothing else.
 *
 * Cornerstone's `defaultAreaGetTextLines` prints five lines -- Area, Mean, Max, Min and
 * Std Dev -- for every rectangle, ellipse and circle. On these surfaces four of those are
 * noise at best and misleading at worst:
 *
 * - **On a photograph they are display values.** An intraoral photo's pixels are sRGB
 *   triples; a "Mean: 143" over a tooth is a number about the JPEG, not about the patient.
 * - **On a CBCT they are not Hounsfield.** CBCT greyscale is vendor-dependent and is not
 *   calibrated HU -- the same anatomy reads differently between machines and between
 *   fields of view on one machine. The roadmap already refuses to *store* those numbers
 *   from the client for this reason (`annotations/adapters/cornerstone.py` keeps the shape
 *   and drops the statistic, and `annotations_compute_roi_stats` supplies it from the
 *   voxels with nibabel's unconditional rescale). Printing them in the overlay while
 *   refusing to store them is the same claim made in two voices.
 * - **Four extra lines per ROI covers the anatomy.** Three ROIs on one slice is fifteen
 *   lines of text over the image.
 *
 * So the overlay shows the area, which is geometry and is true in whatever unit the image
 * has earned -- `px²` uncalibrated, `mm²` once calibrated. The statistics still exist in
 * `cachedStats` for anything that wants them; this only changes what is *drawn*.
 *
 * Written here rather than imported from `createGetTextLines`, which upstream does not
 * publish in its `utilities` barrel: a deep import into `dist/esm` is the kind of thing a
 * patch release moves. `roundNumber` is public and is the only piece actually needed.
 */

/**
 * Build the `getTextLines` an ROI tool's configuration takes.
 *
 * Mirrors upstream's contract: `(data, targetId) => string[] | undefined`, where returning
 * nothing means "no text box yet" (the ROI is still being drawn, or its stats have not
 * been computed). Several target ids arrive as an array when one annotation spans more
 * than one volume, and the first that has an area wins -- upstream de-duplicates equal
 * values across targets, which for a single area line is the same thing.
 *
 * @param {Function} roundNumber `utilities.roundNumber` from `@cornerstonejs/core`.
 * @returns {Function}
 */
export function createAreaOnlyTextLines(roundNumber) {
    return function areaOnlyTextLines(data, targetId) {
        const targetIds = Array.isArray(targetId) ? targetId : [targetId];
        for (const id of targetIds) {
            const stats = data?.cachedStats?.[id];
            const area = stats?.area;
            if (typeof area !== 'number' || !Number.isFinite(area)) {
                continue;
            }
            // The unit is whatever the image earned: `px²` with no pixel spacing, `mm²`
            // with one, and `mm² User` when the spacing came from a user calibration.
            // Taken from the stats rather than assumed, so a calibration change is
            // reflected without this module knowing calibration exists.
            const unit = stats.areaUnit ?? '';
            return [`Area: ${roundNumber(area)} ${unit}`.trimEnd()];
        }
        return undefined;
    };
}

/** The ROI tools this applies to. Length and Angle print one line already. */
export const AREA_TOOLS = Object.freeze(['RectangleROI', 'EllipticalROI', 'CircleROI']);

/**
 * The `toolGroup.addTool` configuration for every area tool.
 *
 * @param {object} tools name -> tool class, as the entries assemble it.
 * @param {Function} roundNumber
 * @returns {Map<string, object>} real tool name -> configuration.
 */
export function areaOnlyConfiguration(tools, roundNumber) {
    const getTextLines = createAreaOnlyTextLines(roundNumber);
    const configuration = new Map();
    for (const name of AREA_TOOLS) {
        if (tools[name]) {
            configuration.set(tools[name].toolName, { getTextLines });
        }
    }
    return configuration;
}
