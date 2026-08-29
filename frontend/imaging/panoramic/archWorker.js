/**
 * Talking to `static/js/worker/seg2pano_worker.js`, which Phase 7 leaves untouched.
 *
 * The worker owns everything that reads the segmentation: the auto axial slice, the
 * mandible mask, its morphological cleanup, the polynomial fit and the slab. That is the
 * half of the panoramic this phase is explicitly not rewriting, so the module talking to
 * it is a client and nothing more.
 *
 * What it adds over `postMessage` is the two rules the old editor kept in globals:
 *
 * **Every request carries an id, and a reply for an old id is dropped.** Dragging the arch
 * and scrubbing Z both re-request geometry, and a slow reply arriving after a newer one
 * would otherwise redraw the arch somebody has already moved on from.
 *
 * **An error before `initialized` is fatal to the worker; one after is not.** A failed
 * init means no segmentation to fit against and the worker is torn down; a failed geometry
 * request means this arch could not be fitted and the next one still can.
 */

/** The worker's URL. A classic script under `static/`, loaded by the browser, not bundled. */
export const WORKER_URL = '/static/js/worker/seg2pano_worker.js';

/**
 * Open a worker and hand it the segmentation.
 *
 * @param {object} options
 * @param {ArrayBuffer} options.segmentation the segmentation NIfTI's bytes.
 * @param {object} options.raw `{dimensions, affine, flipZ}` of the RAS volume.
 * @param {(fraction: number, stage: string) => void} [options.onProgress]
 * @param {(error: Error, fatal: boolean) => void} [options.onError]
 * @param {(geometry: object) => void} [options.onGeometry]
 * @param {(info: object) => void} [options.onReady] `{dimensions, autoZ, flipZ}`.
 * @param {Function} [options.WorkerImpl] injection seam.
 * @returns {{request: Function, terminate: Function, ready: Promise<object>}}
 */
export function openArchWorker({
    segmentation, raw, onProgress = () => {}, onError = () => {},
    onGeometry = () => {}, onReady = () => {}, WorkerImpl = globalThis.Worker,
}) {
    const worker = new WorkerImpl(WORKER_URL);
    let current = 0;
    let ready = false;
    let settle = null;
    const readyPromise = new Promise((resolve, reject) => {
        settle = { resolve, reject };
    });
    // Nothing else awaits this promise on the failure path -- the surface reacts through
    // `onError` -- and an unhandled rejection would surface in the console as a defect in
    // this module rather than as the worker failure it is.
    readyPromise.catch(() => {});

    worker.onerror = (event) => {
        ready = false;
        const error = new Error(event?.message || 'The panoramic geometry worker stopped unexpectedly.');
        settle.reject(error);
        onError(error, true);
    };

    worker.onmessage = (event) => {
        const message = event?.data ?? {};
        // A reply with no id belongs to whatever is in flight; one with a stale id belongs
        // to a request the surface has already moved past.
        if (message.id && message.id !== current) {
            return;
        }
        if (message.type === 'progress') {
            onProgress(message.value, message.stage);
            return;
        }
        if (message.type === 'error') {
            const fatal = !ready;
            const error = new Error(message.message || 'The panoramic geometry worker failed.');
            if (fatal) {
                settle.reject(error);
            }
            onError(error, fatal);
            return;
        }
        if (message.type === 'initialized') {
            ready = true;
            const info = {
                dimensions: message.dimensions,
                autoZ: message.autoZ,
                flipZ: message.flipZ,
            };
            settle.resolve(info);
            onReady(info);
            return;
        }
        if (message.type === 'geometry') {
            onGeometry({
                z: message.z,
                source: message.source,
                polynomial: message.polynomial,
                start: message.start,
                end: message.end,
                controlPoints: message.controlPoints,
                spline: message.spline,
                centerline: message.centerline,
                slab: message.slab,
                mask: message.mask,
            });
        }
    };

    // The buffer is transferred, so the copy is deliberate: the caller memoises the
    // segmentation bytes to re-open the worker after a `pagehide`, and a transferred
    // buffer would come back detached and zero-length.
    const buffer = segmentation.slice(0);
    current += 1;
    worker.postMessage(
        {
            type: 'init',
            id: current,
            buffer,
            raw: { dimensions: raw.dimensions, affine: raw.affine, flipZ: raw.flipZ },
        },
        [buffer]
    );

    return {
        /**
         * Ask for the arch on one slice.
         *
         * @param {number} z
         * @param {number[][]|null} controlPoints null for the automatic fit.
         * @returns {number} the request id, which the caller can compare to drop its own
         *   stale work the way this module drops the worker's.
         */
        request(z, controlPoints = null) {
            current += 1;
            worker.postMessage({ type: 'geometry', id: current, z, controlPoints: controlPoints || null });
            return current;
        },
        get currentRequest() {
            return current;
        },
        terminate() {
            ready = false;
            worker.terminate();
        },
        ready: readyPromise,
    };
}
