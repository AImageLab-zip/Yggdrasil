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
/** Do two planes of the same length hold the same thing? */
function sameMask(a, b) {
    for (let index = 0; index < a.length; index += 1) {
        if (a[index] !== b[index]) {
            return false;
        }
    }
    return true;
}

export function createMaskStore({ width, height }) {
    if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
        throw new Error(`A frame has positive integer dimensions, got ${width}x${height}.`);
    }
    /** @type {Map<number, Map<string, Uint8Array>>} */
    const frames = new Map();
    /**
     * Which tool last wrote each mask, keyed the same way.
     *
     * Beside the planes rather than inside them: a plane is a typed array the editor hands
     * straight to Cornerstone, and attribution is metadata about who wrote it. Kept in
     * step with `frames` by every mutator here -- a tool for a mask that no longer exists
     * would show up in the annotation list as a row with nothing behind it.
     *
     * @type {Map<number, Map<string, string>>}
     */
    const tools = new Map();

    function forgetTool(timeMs, regionCode) {
        const known = tools.get(timeMs);
        known?.delete(regionCode);
        if (known && known.size === 0) {
            tools.delete(timeMs);
        }
    }

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

        /**
         * Which tool last wrote one mask, or `null` when the record does not say.
         *
         * `null` is the honest answer for every mask stored before attribution existed:
         * the tool was never recorded anywhere, so the annotation list shows the row
         * without a tool rather than guessing one.
         */
        toolAt(timeMs, regionCode) {
            return tools.get(timeMs)?.get(regionCode) ?? null;
        },

        /**
         * Replace one region's mask on one frame, dropping it when it is empty.
         *
         * **The attribution is only rewritten when the mask actually changed**, and this
         * is the place that can tell: the caller reads Cornerstone's buffer back on every
         * frame change for every region, so it has no idea which of them the reader
         * touched. Recording the armed tool unconditionally would relabel every mask on
         * the frame with whatever tool happened to be selected while the reader scrubbed
         * past it. The comparison replaces the copy that was being made anyway when
         * nothing moved.
         *
         * @param {number} timeMs
         * @param {string} regionCode
         * @param {ArrayLike<number>} mask
         * @param {string} [tool] the toolbar key now armed, recorded only if this write
         *   changes the mask.
         * @returns {boolean} whether the mask changed.
         */
        set(timeMs, regionCode, mask, tool) {
            if (mask.length !== width * height) {
                throw new Error(
                    `A ${width}x${height} frame needs ${width * height} values, got ` +
                        `${mask.length}.`
                );
            }
            const previous = frames.get(timeMs)?.get(regionCode) ?? null;
            if (isEmpty(mask)) {
                if (!previous) {
                    return false;
                }
                const regions = frames.get(timeMs);
                regions.delete(regionCode);
                if (regions.size === 0) {
                    frames.delete(timeMs);
                }
                forgetTool(timeMs, regionCode);
                return true;
            }
            if (previous && sameMask(previous, mask)) {
                return false;
            }
            if (!frames.has(timeMs)) {
                frames.set(timeMs, new Map());
            }
            frames.get(timeMs).set(regionCode, Uint8Array.from(mask));
            if (tool) {
                if (!tools.has(timeMs)) {
                    tools.set(timeMs, new Map());
                }
                tools.get(timeMs).set(regionCode, tool);
            }
            return true;
        },

        /**
         * Move every mask from one region code to another, for a rename.
         *
         * The archive is keyed by code, so a rename that left the masks behind would
         * store them under a name the project no longer has.
         */
        rename(fromCode, toCode) {
            if (!fromCode || !toCode || fromCode === toCode) {
                return false;
            }
            for (const [timeMs, regions] of frames) {
                const mask = regions.get(fromCode);
                if (!mask) {
                    continue;
                }
                regions.delete(fromCode);
                regions.set(toCode, mask);
                const tool = tools.get(timeMs)?.get(fromCode);
                forgetTool(timeMs, fromCode);
                if (tool) {
                    if (!tools.has(timeMs)) {
                        tools.set(timeMs, new Map());
                    }
                    tools.get(timeMs).set(toCode, tool);
                }
            }
            return true;
        },

        /** Drop every mask for one region, for a region type that was deleted. */
        forget(regionCode) {
            for (const [timeMs, regions] of [...frames]) {
                if (!regions.delete(regionCode)) {
                    continue;
                }
                forgetTool(timeMs, regionCode);
                if (regions.size === 0) {
                    frames.delete(timeMs);
                }
            }
        },

        /** Fill the store from a state response. */
        load(stateFrames) {
            frames.clear();
            tools.clear();
            for (const frame of stateFrames ?? []) {
                for (const [code, entry] of Object.entries(frame.regions ?? {})) {
                    const mask = decodeRuns(entry.rle ?? [], width, height);
                    if (!isEmpty(mask)) {
                        if (!frames.has(frame.timeMs)) {
                            frames.set(frame.timeMs, new Map());
                        }
                        frames.get(frame.timeMs).set(code, mask);
                        if (entry.tool) {
                            if (!tools.has(frame.timeMs)) {
                                tools.set(frame.timeMs, new Map());
                            }
                            tools.get(frame.timeMs).set(code, entry.tool);
                        }
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
                store.regionsAt(timeMs).map((code) => {
                    const tool = store.toolAt(timeMs, code);
                    return [
                        code,
                        {
                            rle: encodeRuns(store.peek(timeMs, code)),
                            // Omitted rather than sent as null when unknown: the server
                            // records attribution it was told, and an explicit null would
                            // overwrite what an earlier revision knew.
                            ...(tool ? { tool } : {}),
                        },
                    ];
                })
            ),
        })),
        prompts,
    };
}
