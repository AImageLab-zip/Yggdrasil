/**
 * The `yggframe:` image loader: a frame already in memory, as a Cornerstone image.
 *
 * The photo surfaces load JPEG/PNG over HTTP (`yggweb:`); a video frame has no URL, only
 * pixels the browser decoded, so this loader answers from a registry the surface fills
 * after it grabs the frame.
 *
 * It reuses `webImageLoader`'s `repackRgba` and `buildImageObject` rather than building an
 * image object of its own, because those are where the hard-won rules live: greyscale
 * detection over *every* pixel, `numberOfComponents` of 1 or 3 never 4, and `null` pixel
 * spacing so lengths report in pixels instead of a fabricated millimetre.
 *
 * The imageId carries the instance and the frame's time, and **is never persisted**:
 * runtime ids are session-scoped (CONTRIBUTING, imaging invariants). What is stored is the
 * file id and `timeMs`.
 */

import { buildImageObject, repackRgba } from '../loaders/webImageLoader.js';

export const FRAME_IMAGE_SCHEME = 'yggframe';

/** @returns {string} */
export function frameImageId(instanceId, timeMs) {
    return `${FRAME_IMAGE_SCHEME}:${instanceId}:${timeMs}`;
}

/**
 * @param {object} options
 * @param {Map<string, {rgba: Uint8ClampedArray, width: number, height: number}>} options.frames
 * @param {Function} options.voxelManagerFactory `VoxelManager.createImageVoxelManager`.
 */
export function createFrameImageLoader({ frames, voxelManagerFactory }) {
    return function loadFrameImage(imageId) {
        const promise = (async () => {
            const frame = frames.get(imageId);
            if (!frame) {
                throw new Error(`No frame is registered for ${imageId}`);
            }
            const { pixelData, numberOfComponents } = repackRgba(
                frame.rgba,
                frame.width * frame.height
            );
            return buildImageObject({
                imageId,
                width: frame.width,
                height: frame.height,
                pixelData,
                numberOfComponents,
                voxelManagerFactory,
            });
        })();
        return { promise, cancelFn: () => {} };
    };
}
