/**
 * Turning a drawn line of known real length into millimetres per pixel.
 *
 * Pure, and deliberately thin: the server recomputes all of this from the same two
 * points (`common/imaging_calibration.py`) and ignores whatever the client calculated.
 * What lives here is the *preview* -- the number shown while the user is still deciding
 * whether the line they drew is the one they meant -- plus the refusals, so an
 * unusable calibration is caught before a request rather than after one.
 *
 * **Two points give one scalar.** A single line cannot distinguish horizontal from
 * vertical scale, so this reports one number and the record stores it in both axes with
 * `source: "known_length"` saying where it came from. Writing it into `x_mm` and `y_mm`
 * while implying each had been measured would be a fiction with two decimal places.
 */

/** Below this, the division is dominated by where the two clicks landed. */
export const MIN_PIXEL_DISTANCE = 1.0;

/**
 * Millimetres per pixel from two image points and the real distance between them.
 *
 * @param {object} options
 * @param {number[]} options.pointA `[x, y]` in image pixels.
 * @param {number[]} options.pointB `[x, y]` in image pixels.
 * @param {number} options.knownLengthMm the real distance, in millimetres.
 * @returns {{mmPerPixel: number, pixelDistance: number}}
 * @throws {Error} with the message to show, on anything unusable.
 */
export function pixelSpacingFromKnownLength({ pointA, pointB, knownLengthMm }) {
    const a = assertPoint(pointA, 'The first point');
    const b = assertPoint(pointB, 'The second point');

    if (typeof knownLengthMm !== 'number' || !Number.isFinite(knownLengthMm)) {
        throw new Error('Enter the real length of the line, in millimetres.');
    }
    if (knownLengthMm <= 0) {
        throw new Error('The real length must be greater than zero.');
    }

    const pixelDistance = Math.hypot(b[0] - a[0], b[1] - a[1]);
    if (pixelDistance < MIN_PIXEL_DISTANCE) {
        throw new Error(
            'Draw a longer line. A line under a pixel long would give a scale decided ' +
                'by where the two clicks landed rather than by the anatomy.'
        );
    }

    return { mmPerPixel: knownLengthMm / pixelDistance, pixelDistance };
}

function assertPoint(point, label) {
    if (!Array.isArray(point) || point.length !== 2) {
        throw new Error(`${label} must be two image coordinates.`);
    }
    for (const ordinate of point) {
        if (typeof ordinate !== 'number' || !Number.isFinite(ordinate)) {
            throw new Error(`${label} must be two finite numbers.`);
        }
    }
    return point;
}

/**
 * The POST body for the calibration endpoint.
 *
 * The scale is **not** sent. The server derives it from the points and ignores a client
 * value; sending one anyway would create a second apparent source of truth that a later
 * reader would have to discover is ignored.
 *
 * @param {object} options as {@link pixelSpacingFromKnownLength}.
 * @returns {object}
 */
export function calibrationRequest({ pointA, pointB, knownLengthMm }) {
    pixelSpacingFromKnownLength({ pointA, pointB, knownLengthMm });
    return { pointA, pointB, knownLengthMm };
}

/**
 * How a stored calibration reads on screen.
 *
 * Names the provenance because a millimetre on a photograph is only as good as the line
 * somebody drew: "0.142 mm/px" alone invites more confidence than it has earned.
 *
 * @param {object|null} record `metadata['pixel_spacing_mm']`, or null.
 * @returns {string} empty when the image is uncalibrated.
 */
export function formatCalibration(record) {
    if (!record || typeof record.x_mm !== 'number' || !Number.isFinite(record.x_mm)) {
        return '';
    }
    const scale = `${record.x_mm.toPrecision(3)} mm/px`;
    const who = record.calibrated_by ? ` by ${record.calibrated_by}` : '';
    if (!record.known_length_mm) {
        return `${scale}${who}`;
    }
    return `${scale} (from ${record.known_length_mm} mm${who})`;
}

/**
 * The warning shown before a recalibration that would change existing readings.
 *
 * Recalibration is allowed -- the usual reason for it is that the first attempt was
 * wrong, and refusing would leave deleting the work as the only fix. What it must not be
 * is silent: the stored numbers are pixels and stay correct, but their millimetre
 * *reading* changes underneath measurements somebody has already looked at.
 *
 * @param {number} affectedMeasurements
 * @returns {string} empty when nothing is affected.
 */
export function recalibrationWarning(affectedMeasurements) {
    if (!affectedMeasurements) {
        return '';
    }
    const plural = affectedMeasurements === 1 ? 'measurement' : 'measurements';
    return (
        `${affectedMeasurements} saved ${plural} on this image will be reported in ` +
        'different millimetres. The shapes are unchanged; only the scale they are read ' +
        'through has moved.'
    );
}
