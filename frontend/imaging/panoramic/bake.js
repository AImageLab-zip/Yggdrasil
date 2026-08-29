/**
 * Baking the two strips the export ships.
 *
 * **`static/js/seg2pano_core.js` does the arithmetic and is not touched.** Decision #8
 * requires the PNGs collected by `common/export_catalog.py:232-241` to keep their bytes,
 * and the only way to be certain of that is for this phase to change nothing about how
 * they are computed. So this module is a driver: it walks the columns, hands each to
 * `projectColumnPair`, and turns the two float buffers into canvases exactly as the Konva
 * editor did.
 *
 * Two things it keeps from that editor, both deliberate:
 *
 * **Chunked, not blocking.** A CBCT is hundreds of columns of 41-sample slab integration
 * on the main thread. Done in one pass the tab stops responding for seconds, and the
 * generation cannot be cancelled when the reader drags the arch again.
 *
 * **Cancellable by token.** Every run takes the token it started with; a newer run
 * invalidates it. Without that, dragging twice quickly finishes two bakes and the second
 * to *finish* wins rather than the second to start.
 *
 * The core, the scheduler and the document all arrive by injection, so the whole thing is
 * exercised without a browser.
 */

/** Columns per scheduler tick. The Konva editor's number, kept: it is a latency budget. */
export const COLUMNS_PER_CHUNK = 4;

/**
 * Project the volume along the arch, into a MIP and a clipped ray sum.
 *
 * @param {object} options
 * @param {object} options.descriptor `{data, dimensions, flipZ, slope, intercept}`.
 * @param {number[][][]} options.slab the worker's slab: one sample column per output column.
 * @param {object} options.core `Seg2PanoCore`.
 * @param {(fraction: number) => void} [options.onProgress]
 * @param {() => boolean} [options.cancelled] polled between chunks.
 * @param {(callback: Function) => void} [options.schedule] defaults to a macrotask.
 * @returns {Promise<{mip: Float32Array, raysum: Float32Array, width: number,
 *   height: number}|null>} null when the run was cancelled.
 */
export function projectStrips({
    descriptor, slab, core, onProgress = () => {}, cancelled = () => false,
    schedule = (callback) => setTimeout(callback, 0),
}) {
    const width = slab.length;
    const height = descriptor.dimensions.depth;
    if (!width) {
        return Promise.reject(new Error('The arch produced no columns to project.'));
    }
    const mip = new Float32Array(width * height);
    const raysum = new Float32Array(width * height);
    let column = 0;

    return new Promise((resolve, reject) => {
        function chunk() {
            if (cancelled()) {
                resolve(null);
                return;
            }
            try {
                const end = Math.min(width, column + COLUMNS_PER_CHUNK);
                for (; column < end; column += 1) {
                    core.projectColumnPair(
                        descriptor.data, descriptor.dimensions, slab, column, mip, raysum,
                        descriptor.flipZ, descriptor.slope, descriptor.intercept
                    );
                }
            } catch (error) {
                reject(error);
                return;
            }
            onProgress(column / width);
            if (column < width) {
                schedule(chunk);
                return;
            }
            resolve({ mip, raysum, width, height });
        }
        schedule(chunk);
    });
}

/**
 * One float buffer as a greyscale canvas.
 *
 * `normalizeOpenCV` is the core's own min/max scaling, including its `Math.trunc` rather
 * than rounding -- a NumPy `astype(uint8)` quirk the reference implementation has, and
 * which the stored PNGs were produced with.
 */
export function stripCanvas(values, width, height, core, doc = globalThis.document) {
    const normalized = core.normalizeOpenCV(values);
    const canvas = doc.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d');
    const image = context.createImageData(width, height);
    for (let index = 0; index < normalized.length; index += 1) {
        const offset = index * 4;
        image.data[offset] = normalized[index];
        image.data[offset + 1] = normalized[index];
        image.data[offset + 2] = normalized[index];
        image.data[offset + 3] = 255;
    }
    context.putImageData(image, 0, 0);
    return canvas;
}

/**
 * A canvas as a PNG blob.
 *
 * `toBlob` reports failure by handing back null rather than throwing, so the null is turned
 * into a rejection here; a save that posted `undefined` as a file would fail at the server
 * with a message about PNG magic bytes.
 */
export function canvasBlob(canvas) {
    return new Promise((resolve, reject) => {
        canvas.toBlob((blob) => {
            if (blob) {
                resolve(blob);
            } else {
                reject(new Error('The browser could not encode a panoramic PNG.'));
            }
        }, 'image/png');
    });
}
