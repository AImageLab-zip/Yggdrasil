/**
 * A still frame from a `<video>`, addressed by time in milliseconds.
 *
 * ## What this refuses to do
 *
 * It does not guess the frame rate. A browser cannot read one from a `<video>`, and a wrong
 * fps puts every mask on the wrong frame while looking entirely right, so the caller must
 * hand over the server's own `ffprobe` result (`fps`, `width`, `height`) or not mount.
 *
 * ## How a frame is addressed
 *
 * A frame is `n = floor(t * fps)` -- the picture on screen while paused at time `t`. It is
 * stored under `timeMs = round(n * 1000 / fps)`, not under `n`: a re-encode that changes the
 * frame rate moves frame numbers and leaves the clock where it was.
 *
 * To read frame `n` the video is sought to the **middle** of its interval, `(n + 0.5) / fps`.
 * Seeking to `n / fps` lands on the boundary, where the decoder is free to answer with
 * either neighbour; the middle is unambiguous. Measured: 0 wrong frames in 90 seeks at
 * three different offsets (VP8, 25 fps, ~5 ms each). Seeking needs HTTP Range support from
 * the server, which the file-serve endpoint has. `requestVideoFrameCallback` is **not**
 * used: after a seek on a paused video it never fired in headless Chromium, where the
 * `seeked` event always did.
 */

/** Tolerance so a time that is a whole number of frames minus float noise rounds up. */
const EPSILON = 1e-6;

/**
 * Which frame a video time falls in, and where to seek to read it.
 *
 * @param {number} timeSeconds `video.currentTime`.
 * @param {number} fps the probe's frame rate.
 * @returns {{frameIndex: number, timeMs: number, seekSeconds: number}}
 */
export function snapToFrame(timeSeconds, fps) {
    if (!Number.isFinite(fps) || fps <= 0) {
        throw new Error('A frame cannot be addressed without a frame rate.');
    }
    if (!Number.isFinite(timeSeconds) || timeSeconds < 0) {
        throw new Error(`Not a video time: ${timeSeconds}`);
    }
    const frameIndex = Math.floor(timeSeconds * fps + EPSILON);
    return {
        frameIndex,
        timeMs: Math.round((frameIndex * 1000) / fps),
        seekSeconds: (frameIndex + 0.5) / fps,
    };
}

/** The subsampled frame whose timestamp is closest to a paused playback time. */
export function snapToNearestFrame(timeSeconds, fps, frameCount = null) {
    const frame = snapToFrame(Math.max(0, timeSeconds), fps);
    const frameIndex = Math.max(
        0,
        frameCount ? Math.min(Math.round(timeSeconds * fps), frameCount - 1) : Math.round(timeSeconds * fps)
    );
    return {
        ...frame,
        frameIndex,
        timeMs: Math.round((frameIndex * 1000) / fps),
        seekSeconds: (frameIndex + 0.5) / fps,
    };
}

/**
 * Wait for one event with a deadline, so a seek that never completes is an error rather
 * than a page that waits forever.
 */
function once(target, eventName, timeoutMs, { setTimeoutImpl, clearTimeoutImpl }) {
    return new Promise((resolve, reject) => {
        const timer = setTimeoutImpl(() => {
            target.removeEventListener(eventName, onEvent);
            reject(new Error(`The video did not finish seeking within ${timeoutMs} ms.`));
        }, timeoutMs);
        function onEvent() {
            clearTimeoutImpl(timer);
            resolve();
        }
        target.addEventListener(eventName, onEvent, { once: true });
    });
}

/**
 * @param {object} options
 * @param {HTMLVideoElement} options.video
 * @param {object} options.probe `{fps, width, height, frameCount}` from the server.
 * @param {Function} options.createCanvas `(width, height) => canvas`.
 * @param {number} [options.timeoutMs]
 */
export function createFrameSource({
    video,
    probe,
    createCanvas,
    timeoutMs = 5000,
    setTimeoutImpl = globalThis.setTimeout,
    clearTimeoutImpl = globalThis.clearTimeout,
}) {
    const { fps, width, height, frameCount } = probe;
    // A decoded HD frame is several MiB. Keep the active frame and a few likely next frames
    // warm without letting timeline navigation turn into an unbounded image cache.
    const frameCache = new Map();
    const pendingFrames = new Map();
    const maxCachedFrames = 5;
    let readQueue = Promise.resolve();

    async function waitForMetadata() {
        if (video.videoWidth && video.videoHeight) {
            return;
        }
        await once(video, 'loadedmetadata', timeoutMs, { setTimeoutImpl, clearTimeoutImpl });
    }

    function remember(frame) {
        frameCache.delete(frame.frameIndex);
        frameCache.set(frame.frameIndex, frame);
        while (frameCache.size > maxCachedFrames) {
            frameCache.delete(frameCache.keys().next().value);
        }
        return frame;
    }

    async function readFrame(frameIndex) {
        // The probe describes the *file*; the element reports what the browser decoded.
        // They disagree only if the wrong file is loaded, and masks drawn on a grid of
        // another size would be stored against pixels they do not describe.
        await waitForMetadata();
        if (video.videoWidth !== width || video.videoHeight !== height) {
            throw new Error(
                `The video is ${video.videoWidth}x${video.videoHeight} but its probe says ` +
                    `${width}x${height}; refusing to annotate a different grid.`
            );
        }
        const target = snapToFrame((frameIndex + 0.5) / fps, fps);
        video.pause?.();
        const seeked = once(video, 'seeked', timeoutMs, { setTimeoutImpl, clearTimeoutImpl });
        video.currentTime = target.seekSeconds;
        await seeked;

        const canvas = createCanvas(width, height);
        const context = canvas.getContext('2d', { willReadFrequently: true });
        context.drawImage(video, 0, 0, width, height);
        const { data } = context.getImageData(0, 0, width, height);
        return remember({ rgba: data, width, height, frameIndex: target.frameIndex, timeMs: target.timeMs });
    }

    function loadFrame(frameIndex) {
        const clampedIndex = Number.isInteger(frameCount)
            ? Math.max(0, Math.min(frameIndex, frameCount - 1))
            : Math.max(0, frameIndex);
        if (frameCache.has(clampedIndex)) {
            return Promise.resolve(frameCache.get(clampedIndex));
        }
        if (pendingFrames.has(clampedIndex)) {
            return pendingFrames.get(clampedIndex);
        }
        const task = readQueue.then(() => readFrame(clampedIndex));
        // A failed prefetch must not prevent a later explicit selection from seeking.
        readQueue = task.catch(() => {});
        pendingFrames.set(clampedIndex, task);
        task.then(
            () => pendingFrames.delete(clampedIndex),
            () => pendingFrames.delete(clampedIndex)
        );
        return task;
    }

    return {
        probe,

        /** The frame the paused video is showing now. */
        currentFrame() {
            return snapToFrame(video.currentTime, fps);
        },

        nearestFrame(timeSeconds) {
            return snapToNearestFrame(timeSeconds, fps, frameCount);
        },

        /**
         * Pixels of one frame.
         *
         * @param {number} frameIndex
         * @returns {Promise<{rgba: Uint8ClampedArray, width: number, height: number,
         *   frameIndex: number, timeMs: number}>}
         */
        async grab(frameIndex) {
            return loadFrame(frameIndex);
        },

        /** Warm likely next/previous frames without changing the visible annotation. */
        prefetch(frameIndexes) {
            return Promise.all(frameIndexes.map((frameIndex) => loadFrame(frameIndex)));
        },
    };
}
