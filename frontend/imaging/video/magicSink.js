/**
 * Where the Magic Tool's masks land now.
 *
 * **Decision #9 freezes the WebSocket half**: `static/js/laparoscopy/laparoscopy_annotator_worker.js`
 * and the Django proxies at `laparoscopy/views.py` are untouched by this phase, because
 * that path does something no in-browser controller does -- temporal propagation and
 * track identity across a video window. Replacing it would be a capability regression on
 * the one surface that uses it most, which is why `@cornerstonejs/ai` stays deferred
 * (`docs/cornerstone-future-work.md` #1).
 *
 * What changes is only the **sink**. The worker returns a mask; the old annotator traced
 * that mask's outer contour into a Konva polygon, because a polygon was the only thing
 * it could store. The record is a labelmap now, so the mask is written as a mask and the
 * tracing is deleted -- along with the component filtering that existed to make a traced
 * contour look reasonable. That filtering was compensating for the representation, not
 * for the model: dropping a 40-pixel island because it would have made an ugly polygon
 * threw away a real, if small, prediction.
 *
 * Pure: masks in, masks out. The transport, the session handshake and the prompt
 * bookkeeping all stay where they are.
 */

/**
 * Fold a returned mask into a region's plane.
 *
 * @param {object} options
 * @param {Uint8Array} options.plane the region's current mask, mutated in place.
 * @param {ArrayLike<number>} options.mask what the worker returned, same dimensions.
 * @param {'replace'|'add'|'subtract'} [options.mode]
 * @returns {Uint8Array} the same plane, for chaining.
 */
export function applyWorkerMask({ plane, mask, mode = 'replace' }) {
    if (mask.length !== plane.length) {
        throw new Error(
            `The worker returned ${mask.length} values for a ${plane.length}-pixel ` +
                'frame. Resizing it here would move every boundary by an amount nobody ' +
                'chose; the session was opened against a different frame size.'
        );
    }
    for (let index = 0; index < plane.length; index += 1) {
        const value = mask[index] ? 1 : 0;
        if (mode === 'replace') {
            plane[index] = value;
        } else if (mode === 'add') {
            plane[index] = plane[index] || value;
        } else {
            // Destructive, per decision #14. The mask is the record; there is no stroke
            // log to replay, and the revision chain is the audit trail.
            plane[index] = value ? 0 : plane[index];
        }
    }
    return plane;
}

/**
 * Turn the worker's flat byte string into the plane the store holds.
 *
 * The worker speaks in `0`/`255`; anything non-zero is inside the mask. Thresholding at
 * "not zero" rather than at 128 is deliberate -- a soft SAM2 boundary should be included
 * or the region shrinks by a pixel on every propagation step.
 *
 * @param {ArrayLike<number>} bytes
 * @param {number} width
 * @param {number} height
 * @returns {Uint8Array}
 */
export function maskFromWorkerBytes(bytes, width, height) {
    const expected = width * height;
    if (bytes.length !== expected) {
        throw new Error(
            `The worker returned ${bytes.length} bytes for a ${width}x${height} frame.`
        );
    }
    const out = new Uint8Array(expected);
    for (let index = 0; index < expected; index += 1) {
        out[index] = bytes[index] ? 1 : 0;
    }
    return out;
}
