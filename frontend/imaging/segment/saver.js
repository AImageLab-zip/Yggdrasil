/**
 * Autosave for revisioned work: debounce, one request in flight, and a 409 is final.
 *
 * Generic over *what* is saved -- it tracks dirty keys (a frame's `timeMs`, say) and hands
 * the set to `send`, which builds and posts the request. That keeps this file about timing
 * and ordering, which is where the bugs were in the tooth editor it is modelled on, and out
 * of encoding.
 *
 * ## The rules
 *
 * - **One request in flight.** Two concurrent saves would both quote the same revision and
 *   the second would 409 against the first, so a save that arrives mid-flight waits.
 * - **A version guard.** Each `markDirty` bumps a per-key version. A key stays dirty after a
 *   save only if it changed *while that save was in flight*, so an edit made during the
 *   request is never marked clean by a response that did not include it.
 * - **A 409 is terminal.** The client's copy is stale; retrying would overwrite somebody
 *   else's work. Autosave stops and the surface says to reload.
 * - **Any other failure keeps the work dirty** and does not retry on its own -- the next
 *   edit or an explicit save does. A tight retry loop against a down server helps nobody.
 */

export const SAVE_DELAY_MS = 1500;

export const SaveStatus = Object.freeze({
    CLEAN: 'clean',
    DIRTY: 'dirty',
    SAVING: 'saving',
    CONFLICT: 'conflict',
    ERROR: 'error',
});

/**
 * @param {object} options
 * @param {(keys: string[], expectedRevision: number) => Promise<{revision: number}>} options.send
 *   Resolve with the new revision; reject with an error whose `conflict` is true on a 409.
 * @param {number} [options.revision] the revision the client loaded.
 * @param {(status: string, detail?: object) => void} [options.onStatus]
 */
export function createSaver({
    send,
    revision = 0,
    onStatus = () => {},
    delayMs = SAVE_DELAY_MS,
    setTimeoutImpl = globalThis.setTimeout,
    clearTimeoutImpl = globalThis.clearTimeout,
}) {
    /** key -> version of the last edit */
    const dirty = new Map();
    let version = 0;
    let currentRevision = revision;
    let timer = null;
    let inFlight = null;
    let status = SaveStatus.CLEAN;

    function setStatus(next, detail) {
        status = next;
        onStatus(next, detail);
    }

    function clearTimer() {
        if (timer !== null) {
            clearTimeoutImpl(timer);
            timer = null;
        }
    }

    function schedule() {
        clearTimer();
        timer = setTimeoutImpl(() => {
            timer = null;
            void run();
        }, delayMs);
    }

    async function run() {
        if (status === SaveStatus.CONFLICT) {
            return;
        }
        if (inFlight) {
            // Whoever started the flight reschedules when it lands.
            return inFlight;
        }
        if (dirty.size === 0) {
            return;
        }
        const sent = new Map(dirty);
        setStatus(SaveStatus.SAVING);
        inFlight = (async () => {
            try {
                const result = await send([...sent.keys()], currentRevision);
                currentRevision = result.revision;
                for (const [key, sentVersion] of sent) {
                    if (dirty.get(key) === sentVersion) {
                        dirty.delete(key);
                    }
                }
                setStatus(dirty.size ? SaveStatus.DIRTY : SaveStatus.CLEAN, {
                    revision: currentRevision,
                });
                if (dirty.size) {
                    schedule();
                }
            } catch (error) {
                if (error?.conflict) {
                    clearTimer();
                    setStatus(SaveStatus.CONFLICT, { error });
                } else {
                    setStatus(SaveStatus.ERROR, { error });
                }
            } finally {
                inFlight = null;
            }
        })();
        return inFlight;
    }

    return {
        /** Record an edit to `key` and (re)start the debounce. */
        markDirty(key) {
            if (status === SaveStatus.CONFLICT) {
                return;
            }
            version += 1;
            dirty.set(String(key), version);
            if (status !== SaveStatus.SAVING) {
                setStatus(SaveStatus.DIRTY);
            }
            schedule();
        },

        /** Save now: cancels the debounce, waits for any flight, then sends what is dirty. */
        async flush() {
            clearTimer();
            if (inFlight) {
                await inFlight;
            }
            await run();
            // An edit that landed during the flight we just awaited is sent too.
            if (dirty.size && status !== SaveStatus.CONFLICT && status !== SaveStatus.ERROR) {
                await run();
            }
            return status;
        },

        get status() {
            return status;
        },
        get revision() {
            return currentRevision;
        },
        get pending() {
            return dirty.size;
        },
        destroy() {
            clearTimer();
        },
    };
}
