/**
 * The video's region masks: run lengths on the wire, `Uint8Array` planes in memory.
 *
 * Pure. Nothing here imports Cornerstone or touches a DOM, so every decision that can be
 * wrong about a pixel is testable without a browser.
 *
 * ## The wire format, and why it is this one
 *
 * A flat list of run lengths, row-major, **always opening with a run of zeros**. So an
 * empty mask is `[width * height]` and a full one is `[0, width * height]`. Chosen over
 * raw bytes because a 1080p frame is two megabytes and a surgical mask is overwhelmingly
 * one colour; chosen over PNG because a mask is not an image, and round-tripping one
 * through a codec invites a lossy setting nobody notices until a boundary moves.
 *
 * `decodeRuns`/`encodeRuns` mirror `decode_rle`/`encode_rle` in
 * `annotations/services/video.py`, and `frontend/tests/videoMasks.test.js` pins them
 * against the same fixtures the Python tests use. Two implementations of a wire format
 * is the usual way a client and a server come to disagree about the last row.
 *
 * ## Regions are keyed by code
 *
 * Not by index into anything. The export's class axis is the project's region types in
 * order, so an index would be re-pointed the first time somebody adds a category —
 * silently re-labelling every historical study. The server stores by code for the same
 * reason.
 */

/**
 * Run lengths to an `(height * width)` mask.
 *
 * Refuses a list that does not cover the frame exactly. Padding a short one would put
 * every pixel after the shortfall on the wrong row, which reads as a mask that drifted
 * rather than as a malformed message.
 *
 * @param {number[]} runs
 * @param {number} width
 * @param {number} height
 * @returns {Uint8Array}
 */
export function decodeRuns(runs, width, height) {
    if (!Array.isArray(runs)) {
        throw new Error('A mask is a list of run lengths.');
    }
    const expected = width * height;
    const out = new Uint8Array(expected);
    let position = 0;
    let value = 0;
    for (let index = 0; index < runs.length; index += 1) {
        const run = runs[index];
        if (!Number.isInteger(run) || run < 0) {
            throw new Error(`Run ${index} must be a non-negative integer, got ${run}.`);
        }
        const end = position + run;
        if (end > expected) {
            throw new Error(
                `Run lengths overflow a ${width}x${height} frame at run ${index}.`
            );
        }
        if (value) {
            out.fill(1, position, end);
        }
        position = end;
        value ^= 1;
    }
    if (position !== expected) {
        throw new Error(
            `Run lengths cover ${position} of ${expected} pixels; the mask does not ` +
                'describe this frame.'
        );
    }
    return out;
}

/**
 * The inverse. A mask whose first pixel is set opens with an explicit empty run.
 *
 * @param {Uint8Array} mask
 * @returns {number[]}
 */
export function encodeRuns(mask) {
    if (!mask?.length) {
        return [];
    }
    const runs = [];
    if (mask[0]) {
        runs.push(0);
    }
    let current = mask[0] ? 1 : 0;
    let length = 0;
    for (let index = 0; index < mask.length; index += 1) {
        const value = mask[index] ? 1 : 0;
        if (value === current) {
            length += 1;
        } else {
            runs.push(length);
            current = value;
            length = 1;
        }
    }
    runs.push(length);
    return runs;
}

/**
 * Is nothing painted?
 *
 * An all-zero plane is the *absence* of an annotation, not an annotation of nothing, so
 * it is dropped rather than stored -- otherwise every save would carry one empty plane
 * per region per frame, multiplying the archive by the size of the project's vocabulary
 * to record that the user did not draw anything.
 */
export function isEmpty(mask) {
    for (let index = 0; index < mask.length; index += 1) {
        if (mask[index]) {
            return false;
        }
    }
    return true;
}

/**
 * Every annotated frame's masks, keyed by instant and then by region code.
 *
 * Deliberately a plain data structure with explicit mutators rather than a class that
 * also talks to Cornerstone. The editor pushes Cornerstone's labelmap buffer in here on
 * every stroke and reads it back out when the frame changes; keeping the two apart is
 * what makes "did the mask survive a frame change" a unit test rather than a browser
 * session.
 */
export function createMaskStore({ width, height }) {
    if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
        throw new Error(`A frame has positive integer dimensions, got ${width}x${height}.`);
    }
    /** @type {Map<number, Map<string, Uint8Array>>} */
    const frames = new Map();

    return {
        width,
        height,

        /** The mask for one region on one frame, creating an empty one on demand. */
        plane(timeMs, regionCode) {
            if (!frames.has(timeMs)) {
                frames.set(timeMs, new Map());
            }
            const regions = frames.get(timeMs);
            if (!regions.has(regionCode)) {
                regions.set(regionCode, new Uint8Array(width * height));
            }
            return regions.get(regionCode);
        },

        /** The mask for one region on one frame, or `null` if nothing is painted there. */
        peek(timeMs, regionCode) {
            return frames.get(timeMs)?.get(regionCode) ?? null;
        },

        /** Which instants carry anything. */
        annotatedTimes() {
            return [...frames.keys()].sort((a, b) => a - b);
        },

        /** Which regions are painted on one instant. */
        regionsAt(timeMs) {
            return [...(frames.get(timeMs)?.keys() ?? [])].sort();
        },

        /** Replace one region's mask on one frame, dropping it when it is empty. */
        set(timeMs, regionCode, mask) {
            if (mask.length !== width * height) {
                throw new Error(
                    `A ${width}x${height} frame needs ${width * height} values, got ` +
                        `${mask.length}.`
                );
            }
            if (isEmpty(mask)) {
                const regions = frames.get(timeMs);
                regions?.delete(regionCode);
                if (regions && regions.size === 0) {
                    frames.delete(timeMs);
                }
                return;
            }
            if (!frames.has(timeMs)) {
                frames.set(timeMs, new Map());
            }
            frames.get(timeMs).set(regionCode, Uint8Array.from(mask));
        },

        /** Fill the store from a state response. */
        load(stateFrames) {
            frames.clear();
            for (const frame of stateFrames ?? []) {
                for (const [code, entry] of Object.entries(frame.regions ?? {})) {
                    const mask = decodeRuns(entry.rle ?? [], width, height);
                    if (!isEmpty(mask)) {
                        if (!frames.has(frame.timeMs)) {
                            frames.set(frame.timeMs, new Map());
                        }
                        frames.get(frame.timeMs).set(code, mask);
                    }
                }
            }
        },
    };
}

/**
 * The PUT body for the whole-state save.
 *
 * The client sends everything it has, because the server owns the whole set and carries
 * nothing forward -- see `save_video_regions`. Sending a delta would make an erase
 * indistinguishable from an omission.
 *
 * @param {object} options
 * @param {ReturnType<createMaskStore>} options.store
 * @param {object[]} options.prompts SAM2 prompt points, already in [0, 1].
 * @param {number} options.expectedRevision the revision the editor read.
 * @returns {object}
 */
export function buildSaveRequest({ store, prompts = [], expectedRevision }) {
    return {
        width: store.width,
        height: store.height,
        expectedRevision,
        frames: store.annotatedTimes().map((timeMs) => ({
            timeMs,
            regions: Object.fromEntries(
                store
                    .regionsAt(timeMs)
                    .map((code) => [code, { rle: encodeRuns(store.peek(timeMs, code)) }])
            ),
        })),
        prompts,
    };
}
