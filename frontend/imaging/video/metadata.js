/**
 * Telling Cornerstone what a Yggdrasil video is: `yggvideo:<url>/frames/<n>`.
 *
 * ## A correction worth recording, because it nearly shipped
 *
 * The first draft of this surface did not use `VideoViewport` at all. It decoded frames
 * through a hidden `<video>` into a custom image loader and annotated them on a
 * `StackViewport`, on the conclusion -- written up as a finding, and wrong -- that
 * Cornerstone 5.8.2 could not render a labelmap on a `VideoViewport`. The reasoning was:
 * `VideoViewport extends Viewport`, not `StackViewport`, and
 * `tools/displayTools/Labelmap/.../syncStackLabelmapActors.js` calls
 * `viewport.getImageDataMetadata(...)` and `viewport.addImages(...)`.
 *
 * Both halves are true. The conclusion is not: **`VideoViewport` defines both**
 * (`VideoViewport.js:193` and `:705`), and `addImages` builds a `CanvasActor` per
 * labelmap exactly so this works. The mistake was reading one grep whose output was
 * truncated and not re-reading the file. `frontend/tests/videoLabelmapSupport.test.js`
 * now pins the four facts the *correct* conclusion rests on, so a future bump that
 * removes them fails the build instead of leaving this surface quietly broken.
 *
 * The cost of the wrong version would have been a bespoke frame decoder, a second image
 * loader and a per-frame stack, all to work around a limitation that does not exist --
 * carried forever, because nobody re-reads a workaround that appears to be load-bearing.
 *
 * ## What the viewport actually needs
 *
 * `utilities/VideoUtilities.js::loadVideoStreamMetadata` reads four modules off the
 * imageId, and the shapes below are dictated by that function rather than chosen:
 *
 *   - `imageUrlModule.rendered` -- the URL the `<video>` element gets. Supplying it
 *     explicitly is what frees the imageId from having to be a DICOMweb `/frames/` URL.
 *   - `cineModule.numberOfFrames` and a frame rate, from the server's own `ffprobe`.
 *     A browser cannot report a video's frame rate, so the page states it; guessing 30
 *     would put every annotation on the wrong frame for a 25 fps recording.
 *   - `generalSeriesModule.Modality`.
 *   - `imagePlaneModule.rows`/`columns` -- `getVideoImageDataMetadata` reads these to
 *     size the actor. **`columnPixelSpacing` is deliberately absent**: a laparoscope has
 *     no calibrated scale, and reporting one would let `LengthTool` present millimetres
 *     the image cannot support. That is the same refusal `MeasurementItem.is_calibrated`
 *     makes in the database.
 *
 * The imageId ends in a frame number because `VideoViewport.getImageIds()` derives the
 * per-frame ids by replacing a trailing integer. Without one, every frame would share an
 * id and every frame would share a labelmap.
 */

import { assertLoaderSafeUrl, toAbsoluteUrl } from '../ids/imageIds.js';

/** The imageId scheme. One token, so both of Cornerstone's URI spellings agree. */
export const VIDEO_SCHEME = 'yggvideo';

/** Modality for a video with no DICOM identity. `OT` is the standard "other". */
export const VIDEO_MODALITY = 'OT';

/**
 * The `yggvideo:` imageId for one frame of one video.
 *
 * @param {object} options
 * @param {string} options.url the video's URL, absolute or document-relative.
 * @param {number} [options.frameNumber] 1-based, as DICOM and `VideoViewport` count.
 * @param {string} [options.origin]
 * @returns {string}
 */
export function videoImageId({ url, frameNumber = 1, origin } = {}) {
    if (!Number.isInteger(frameNumber) || frameNumber < 1) {
        throw new Error(
            `Frame numbers are 1-based integers, got ${frameNumber}. Cornerstone counts ` +
                'frames from one and a zero would address the frame before the first.'
        );
    }
    const absolute = assertLoaderSafeUrl(toAbsoluteUrl(url, { origin }));
    return `${VIDEO_SCHEME}:${absolute}/frames/${frameNumber}`;
}

/**
 * `{url, frameNumber}` out of a `yggvideo:` imageId.
 *
 * @param {string} imageId
 * @returns {{url: string, frameNumber: number}}
 */
export function parseVideoImageId(imageId) {
    if (typeof imageId !== 'string') {
        throw new Error('imageId must be a string.');
    }
    const prefix = `${VIDEO_SCHEME}:`;
    if (!imageId.startsWith(prefix)) {
        throw new Error(
            `${JSON.stringify(imageId)} is not a ${prefix} imageId. This provider is ` +
                'registered for that scheme only.'
        );
    }
    const rest = imageId.slice(prefix.length);
    const match = rest.match(/^(.*)\/frames\/(\d+)$/);
    if (!match) {
        throw new Error(
            `${JSON.stringify(imageId)} names no frame. A video imageId must end in ` +
                '/frames/<n>, or every frame would share one id and one labelmap.'
        );
    }
    return { url: match[1], frameNumber: Number(match[2]) };
}

/**
 * The metadata provider to hand `metaData.addProvider`.
 *
 * Pure apart from the closure over `videos`: it answers from what the page already
 * stated, and never fetches. A provider that went to the network would be called
 * synchronously by `loadVideoStreamMetadata` and would return `undefined` every time.
 *
 * @param {Function} lookup `(url) => {width, height, fps, numberOfFrames} | null`
 * @returns {Function} `(type, imageId) => object | undefined`
 */
export function createVideoMetadataProvider(lookup) {
    return function provide(type, imageId) {
        if (typeof imageId !== 'string' || !imageId.startsWith(`${VIDEO_SCHEME}:`)) {
            return undefined;
        }
        let parsed;
        try {
            parsed = parseVideoImageId(imageId);
        } catch {
            return undefined;
        }
        const video = lookup(parsed.url);
        if (!video) {
            return undefined;
        }
        return describeVideo(type, video);
    };
}

/**
 * The module bodies themselves, split out so they are testable without a provider.
 *
 * @param {string} type a `MetadataModules` value.
 * @param {object} video `{url, width, height, fps, numberOfFrames}`
 * @returns {object|undefined}
 */
export function describeVideo(type, video) {
    switch (type) {
        case 'imageUrlModule':
            return { rendered: video.url };
        case 'generalSeriesModule':
            return { Modality: VIDEO_MODALITY, modality: VIDEO_MODALITY };
        case 'cineModule':
            return {
                cineRate: video.fps,
                numberOfFrames: video.numberOfFrames,
                frameTime: video.fps ? 1000 / video.fps : undefined,
            };
        case 'instance':
            return { NumberOfFrames: video.numberOfFrames };
        case 'imagePlaneModule':
            return {
                rows: video.height,
                columns: video.width,
                imageOrientationPatient: [1, 0, 0, 0, 1, 0],
                imagePositionPatient: [0, 0, 0],
                // No pixel spacing. A laparoscope has no calibrated scale, and stating
                // one here would let LengthTool report millimetres for a number that is
                // pixels -- the exact claim `MeasurementItem.is_calibrated` refuses.
                rowPixelSpacing: null,
                columnPixelSpacing: null,
                usingDefaultValues: true,
            };
        default:
            return undefined;
    }
}
