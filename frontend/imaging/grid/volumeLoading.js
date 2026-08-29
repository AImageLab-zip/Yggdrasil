/**
 * Waiting for a Cornerstone volume to actually finish loading.
 *
 * `ImageVolume.load(callback)` is **an empty method returning `undefined`**
 * (`cache/classes/ImageVolume.js:115`), and the streaming subclass that overrides it
 * is callback-based and also returns `undefined`
 * (`BaseStreamingImageVolume.js:165`). So `await volume.load()` awaits `undefined`,
 * resolves on the next microtask, and hands back a volume with no frames in it.
 *
 * That is not a subtle failure. `voxelManager.getCompleteScalarDataArray()` returns
 * `new Uint8Array(0)` when no slice has data yet (`VoxelManager.js:643-647`), so the
 * caller gets an empty array rather than an error, and whatever it computes from that
 * is garbage that looks like data. The first real harness run showed both halves of
 * it: most studies read back `cached 0`, and one — `maxillo/48470` — caught the volume
 * mid-load and reported 182 of 200,000 voxels disagreeing, which reads exactly like a
 * subtle intensity bug and is not one.
 *
 * The completion signal is `framesProcessed === totalNumFrames`, not the first
 * callback: `callLoadStatusCallback` fires **once per frame**
 * (`BaseStreamingImageVolume.js:104`), and individual frames can fail permanently with
 * `success: false` while the rest carry on (`:132-152`). Both facts are encoded in
 * {@link reduceLoadEvent}, which is pure so they can be tested without a GPU.
 */

/** A load that has not completed in this long is reported rather than awaited forever. */
export const DEFAULT_LOAD_TIMEOUT_MS = 5 * 60 * 1000;

/** The initial accumulator for {@link reduceLoadEvent}. */
export function initialLoadState() {
    return { done: false, framesLoaded: 0, framesProcessed: 0, totalNumFrames: 0, failures: [] };
}

/**
 * Fold one load-status event into the running state.
 *
 * Pure. The three rules it encodes, each of which was wrong in the first version:
 *
 *   1. **Completion is `framesProcessed === totalNumFrames`**, not the arrival of a
 *      callback. The callback fires per frame.
 *   2. **A failed frame still counts as processed.** `errorCallback` increments
 *      `framesProcessed` and can drive the volume to "loaded" with frames missing, so
 *      waiting for `framesLoaded` to reach the total would hang forever on a volume
 *      that had one bad frame.
 *   3. **Failures are collected, not thrown at.** A volume that lost 2 of 400 frames
 *      is a different problem from one that lost all of them, and the caller decides.
 *
 * @param {object} state from {@link initialLoadState}.
 * @param {object} event a Cornerstone load-status event.
 * @returns {object} the next state.
 */
export function reduceLoadEvent(state, event) {
    const totalNumFrames = Number(event?.totalNumFrames) || state.totalNumFrames;
    const framesProcessed = Number(event?.framesProcessed) || 0;
    const failures = event?.success === false
        ? [...state.failures, { imageId: event.imageId, error: String(event.error ?? 'unknown') }]
        : state.failures;

    return {
        totalNumFrames,
        framesProcessed: Math.max(state.framesProcessed, framesProcessed),
        framesLoaded: Math.max(state.framesLoaded, Number(event?.framesLoaded) || 0),
        failures,
        done: totalNumFrames > 0 && framesProcessed >= totalNumFrames,
    };
}

/**
 * Describe a finished load, or explain why it is not usable.
 *
 * @param {object} state
 * @returns {{ok: boolean, message: string|null}}
 */
export function describeLoadOutcome(state) {
    if (!state.done) {
        return {
            ok: false,
            message:
                `The volume did not finish loading: ${state.framesProcessed} of ` +
                `${state.totalNumFrames || 'an unknown number of'} frames were processed.`,
        };
    }
    if (state.failures.length) {
        const [first] = state.failures;
        return {
            ok: false,
            message:
                `${state.failures.length} of ${state.totalNumFrames} frames failed to load ` +
                `(first: ${first.imageId ?? 'unknown image'} -- ${first.error}). ` +
                'A volume with missing frames must not be measured: the gaps read as data.',
        };
    }
    return { ok: true, message: null };
}

/**
 * Load a volume and resolve only when every frame has been accounted for.
 *
 * @param {object} volume a Cornerstone `ImageVolume`.
 * @param {object} [options]
 * @param {number} [options.timeoutMs]
 * @returns {Promise<object>} the final load state.
 * @throws {Error} on timeout, or if any frame failed permanently.
 */
export function awaitVolumeLoad(volume, { timeoutMs = DEFAULT_LOAD_TIMEOUT_MS } = {}) {
    return new Promise((resolve, reject) => {
        let state = initialLoadState();
        let settled = false;

        const timer = setTimeout(() => {
            if (settled) {
                return;
            }
            settled = true;
            reject(
                new Error(
                    `The volume did not finish loading within ${Math.round(timeoutMs / 1000)}s ` +
                        `(${state.framesProcessed} of ${state.totalNumFrames} frames). ` +
                        'Refusing to measure a partially loaded volume.'
                )
            );
        }, timeoutMs);

        const finish = () => {
            settled = true;
            clearTimeout(timer);
            const outcome = describeLoadOutcome(state);
            if (outcome.ok) {
                resolve(state);
            } else {
                reject(new Error(outcome.message));
            }
        };

        // `load` takes a callback and returns undefined. It is called once per frame,
        // and once immediately with the totals if the volume is already loaded.
        volume.load((event) => {
            if (settled) {
                return;
            }
            state = reduceLoadEvent(state, event);
            if (state.done) {
                finish();
            }
        });

        // A volume already in the cache never calls back at all in some paths, so the
        // already-loaded case is checked directly rather than waited for.
        if (!settled && volume.loadStatus?.loaded) {
            const total = volume.imageIds?.length ?? 0;
            state = { ...state, done: true, totalNumFrames: total, framesProcessed: total };
            finish();
        }
    });
}

/**
 * Read a volume's cached scalar data, refusing an empty or short array.
 *
 * The guard exists because the empty case is silent: `getCompleteScalarDataArray`
 * returns a zero-length `Uint8Array` when no slice has data, so a caller that trusts
 * it computes statistics over nothing and reports them.
 *
 * @param {object} volume
 * @returns {ArrayLike<number>}
 */
export function readScalarData(volume) {
    const manager = volume?.voxelManager;
    if (!manager) {
        throw new Error('The volume exposes no voxel manager; it has not finished loading.');
    }

    const data = manager.getCompleteScalarDataArray
        ? manager.getCompleteScalarDataArray()
        : manager.getScalarData?.();

    if (!data) {
        throw new Error('The volume exposes no scalar data.');
    }

    const expected = expectedVoxelCount(volume);
    if (data.length === 0) {
        throw new Error(
            'The volume cached no voxels at all. `getCompleteScalarDataArray` returns an ' +
                'empty array when no slice has data yet, so this is a read before the load ' +
                'completed rather than an empty study.'
        );
    }
    if (expected && data.length !== expected) {
        throw new Error(
            `The volume cached ${data.length} voxels but its dimensions imply ${expected}. ` +
                'Measuring a short array would silently treat the missing tail as zeros.'
        );
    }
    return data;
}

function expectedVoxelCount(volume) {
    const dimensions = volume?.dimensions;
    if (!Array.isArray(dimensions) && !ArrayBuffer.isView(dimensions)) {
        return 0;
    }
    return Number(dimensions[0]) * Number(dimensions[1]) * Number(dimensions[2]);
}


/**
 * Read a NIfTI header without downloading the volume.
 *
 * Lives here rather than with the grid because the grid is no longer its only caller: the
 * panoramic needs the same header to reorient the array the baker consumes, and two
 * implementations of "parse this volume's header" is two chances to disagree about an
 * affine.
 *
 * @param {string} url the loader URL.
 * @returns {Promise<object>} the parsed header.
 */
export async function fetchHeader(url) {
    const reader = globalThis.nifti;
    if (!reader) {
        throw new Error('The vendored nifti-reader is not loaded; orientation cannot be checked.');
    }
    // Range-request the header. `serve_file` advertises byte ranges only for audio and
    // video, so a server that ignores the header hands back the whole volume -- which
    // is correct, just larger, and the browser cache absorbs it for the load below.
    const response = await fetch(url, { credentials: 'same-origin', headers: { Range: 'bytes=0-1023' } });
    if (!response.ok && response.status !== 206) {
        throw new Error(`HTTP ${response.status} fetching the volume header.`);
    }
    const buffer = await response.arrayBuffer();
    const decompressed = reader.isCompressed(buffer) ? reader.decompress(buffer) : buffer;
    return reader.readHeader(decompressed);
}
