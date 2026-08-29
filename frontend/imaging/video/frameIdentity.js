/**
 * Which instant a video annotation belongs to.
 *
 * One number, computed one way, on both sides of the wire and on both sides of the
 * migration. That is the whole point of the module: `frame_time_to_ms` in
 * `annotations/adapters/legacy_laparoscopy.py` already made this decision for the
 * converted corpus, and a browser that rounded differently would file a live save one
 * millisecond off a converted one — which, at 30 fps, is the *same* frame most of the
 * time and a different one occasionally. An intermittent off-by-one-frame is the worst
 * kind of bug to be handed.
 *
 * **Rounded, never truncated.** At 30 fps a frame boundary lands on 33.3 ms, so
 * truncation biases every annotation toward the previous frame; over a long operation
 * that drift is a whole frame, and a frame is the unit this surface is addressed by.
 * `frontend/tests/videoFrameIdentity.test.js` pins these against the same fixtures
 * `annotations/tests_adapters.py` uses, so the two cannot drift apart.
 */

/**
 * Seconds (what `HTMLMediaElement.currentTime` and `requestVideoFrameCallback` report)
 * to the integer millisecond the record is keyed by.
 *
 * @param {number} seconds
 * @returns {number}
 */
export function secondsToMs(seconds) {
    if (typeof seconds !== 'number' || !Number.isFinite(seconds)) {
        throw new Error(`A frame time must be a finite number, got ${seconds}.`);
    }
    if (seconds < 0) {
        throw new Error(`A frame time must not be negative, got ${seconds}.`);
    }
    return Math.round(seconds * 1000);
}

/**
 * The instant a `<video>` is displaying right now.
 *
 * Prefers `requestVideoFrameCallback`'s `mediaTime`, which is the presentation timestamp
 * of the frame actually on screen. `currentTime` is the playback clock and can be a few
 * milliseconds ahead of the composited frame during playback, so annotating from it
 * while playing files the mask against the *next* frame. Where the callback is
 * unavailable the clock is the honest fallback, and the surface pauses before annotating
 * anyway, which is when the two agree.
 *
 * @param {HTMLVideoElement} video
 * @param {object} [frameMetadata] the `requestVideoFrameCallback` metadata, if any.
 * @returns {number}
 */
export function currentInstantMs(video, frameMetadata) {
    const seconds =
        typeof frameMetadata?.mediaTime === 'number'
            ? frameMetadata.mediaTime
            : video.currentTime;
    return secondsToMs(seconds);
}

/**
 * The 1-based frame number Cornerstone addresses an instant by.
 *
 * `VideoViewport.setFrameNumber` counts from one, DICOM-style. The record counts in
 * milliseconds. This is the only place the two meet, and it is the same arithmetic
 * `laparoscopy/mask_raster.py::frame_index_for_ms` does on the server -- one off by one
 * (the server's index is 0-based) and no other difference, which is asserted rather than
 * left to be noticed.
 *
 * @param {number} timeMs
 * @param {number} fps
 * @returns {number}
 */
export function frameNumberForMs(timeMs, fps) {
    if (!Number.isFinite(fps) || fps <= 0) {
        throw new Error(
            `A frame number needs a stated frame rate, got ${fps}. A browser cannot ` +
                'report one, so the page must state what the server probed.'
        );
    }
    return Math.round((timeMs / 1000) * fps) + 1;
}

/**
 * The instant a 1-based frame number refers to.
 *
 * @param {number} frameNumber
 * @param {number} fps
 * @returns {number} integer milliseconds.
 */
export function msForFrameNumber(frameNumber, fps) {
    if (!Number.isFinite(fps) || fps <= 0) {
        throw new Error(`A frame time needs a stated frame rate, got ${fps}.`);
    }
    return Math.round(((frameNumber - 1) / fps) * 1000);
}

/**
 * Snap an instant onto the video's frame grid.
 *
 * Two clicks a few milliseconds apart on a paused frame must produce **one** annotated
 * instant, not two masks of the same picture that the export would then OR together.
 * The grid is `fps`, which the page states from the server's own probe rather than the
 * browser guessing -- a browser cannot report a video's frame rate.
 *
 * @param {number} timeMs
 * @param {number} fps
 * @returns {number} the instant of the containing frame, in milliseconds.
 */
export function snapToFrame(timeMs, fps) {
    if (!Number.isFinite(fps) || fps <= 0) {
        // No stated rate: every distinct millisecond is its own instant. Honest, and
        // exactly what the pre-Phase-10 record did.
        return Math.round(timeMs);
    }
    const frameIndex = Math.round((timeMs / 1000) * fps);
    return Math.round((frameIndex / fps) * 1000);
}
