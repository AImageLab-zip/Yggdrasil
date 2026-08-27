/**
 * A Cornerstone image loader for ordinary web images: `yggweb:<absolute url>`.
 *
 * Teleradiography scans and intraoral photographs are JPEGs and PNGs served by Django,
 * not DICOM and not NIfTI. Cornerstone has no loader for them, so this is the ~one
 * screenful that lets a `StackViewport` display one.
 *
 * ## The scheme lost its hyphen, and that is not cosmetic
 *
 * The roadmap wrote `ygg-web:`. `@cornerstonejs/metadata/utilities/imageIdToURI.js`
 * matches `/^[a-zA-Z]+:/`, which a hyphen does not satisfy, so it would return the whole
 * imageId unchanged -- while
 * `@cornerstonejs/tools/utilities/planar/filterAnnotationsForDisplay.js` computes the
 * same thing as `imageId.substring(imageId.indexOf(':') + 1)`. Two spellings of "the URI
 * of this image", disagreeing on every id. It is latent in 5.8.2 (`StackViewport`
 * registers both spellings in `imageKeyToIndexMap`, and the stack branch of
 * `isReferenceViewable` never reads `options.imageURI` when `referencedImageId` is
 * present) and one upstream refactor away from restored annotations silently never
 * drawing. `yggweb:` makes both agree, and `frontend/tests/webImageLoader.test.js`
 * asserts they do.
 *
 * ## Three things read out of the shipped package, each of which fails far from its cause
 *
 * **The loader must build its own `voxelManager`.** `loaders/imageLoader.js:58-70`
 * `ensureVoxelManager` fills one in for an image that lacks it and then does
 * `delete image.imageFrame.pixelData`. A hand-built image has no `imageFrame`, so that
 * is a `TypeError` -- thrown inside `handleImageLoadPromise`'s `.then`, which turns it
 * into a spurious `IMAGE_LOAD_FAILED` event *while the returned promise still resolves*.
 * The viewport then dies later, on `voxelManager.getScalarData()`, with a stack trace
 * that points nowhere near here.
 *
 * **`rowPixelSpacing`/`columnPixelSpacing` must be `null`, not `undefined`.**
 * `StackViewport._checkVTKImageDataMatchesCornerstoneImage` accepts a spacing mismatch
 * only via `image.rowPixelSpacing === null && ySpacing === 1.0`. With `undefined` that
 * test fails, so every step through the stack tears down and rebuilds the actor and
 * resets the VOI -- which reads as "the window keeps jumping back", not as a type bug.
 *
 * **RGB is repacked to three components, not four.** `getImageDataMetadata` derives
 * `numberOfComponents` from the image, and `createActorMapper` sets
 * `setIndependentComponents(false)` above one -- so three is what makes a colour photo
 * render as colour. It is also 25% less memory than the RGBA the canvas hands back.
 */

import {
    assertLoaderSafeUrl,
    assertServableFilename,
    serveFilePath,
    toAbsoluteUrl,
} from '../ids/imageIds.js';

/** The imageId scheme. One token, so both of Cornerstone's URI spellings agree. */
export const WEB_IMAGE_SCHEME = 'yggweb';

/** 8-bit web images only. Nothing here reads a bit depth, so nothing may assume one. */
const MAX_STORED_VALUE = 255;

/**
 * The `yggweb:` imageId for one served file.
 *
 * @param {object} options
 * @param {number} options.fileId the `FileRegistry` row.
 * @param {string} options.filename decorative, but the route requires a segment.
 * @param {string} [options.namespace] one of the serve namespaces.
 * @param {string} [options.origin] defaults to the document origin.
 * @returns {string}
 */
export function webImageId({ fileId, filename, namespace = 'api', origin } = {}) {
    assertServableFilename(filename);
    const url = assertLoaderSafeUrl(
        toAbsoluteUrl(serveFilePath({ fileId, filename, namespace }), { origin })
    );
    return `${WEB_IMAGE_SCHEME}:${url}`;
}

/**
 * The URL inside a `yggweb:` imageId.
 *
 * @param {string} imageId
 * @returns {string} the absolute URL.
 * @throws {Error} on any other scheme -- a loader registered for one scheme being handed
 *   another means the registration is wrong, and guessing would hide that.
 */
export function parseWebImageId(imageId) {
    if (typeof imageId !== 'string') {
        throw new Error('imageId must be a string.');
    }
    const prefix = `${WEB_IMAGE_SCHEME}:`;
    if (!imageId.startsWith(prefix)) {
        throw new Error(
            `${JSON.stringify(imageId)} is not a ${prefix} imageId. This loader is ` +
                'registered for that scheme only.'
        );
    }
    const url = imageId.slice(prefix.length);
    if (!url) {
        throw new Error('A yggweb: imageId must carry a URL.');
    }
    return url;
}

/**
 * The two spellings Cornerstone uses for "the URI of this image", for a given id.
 *
 * Exported so a test can assert they agree rather than trusting the argument in this
 * file's header. `imageIdToURI` is the metadata package's regex; the substring form is
 * what `filterAnnotationsForDisplay` uses.
 *
 * @param {string} imageId
 * @returns {{byRegex: string, bySubstring: string}}
 */
export function uriSpellings(imageId) {
    return {
        byRegex: imageId.replace(/^[a-zA-Z]+:/, ''),
        bySubstring: imageId.substring(imageId.indexOf(':') + 1),
    };
}

/**
 * Assemble the image object a `StackViewport` can display.
 *
 * The pure half of the loader: no fetch, no canvas, no Cornerstone. Everything the
 * header warns about is decided here, which is why it is separately testable.
 *
 * @param {object} options
 * @param {string} options.imageId
 * @param {number} options.width
 * @param {number} options.height
 * @param {Uint8Array} options.pixelData interleaved, `numberOfComponents` per pixel.
 * @param {number} options.numberOfComponents 1 for greyscale, 3 for RGB.
 * @param {Function} options.voxelManagerFactory `VoxelManager.createImageVoxelManager`.
 * @returns {object} a Cornerstone `IImage`.
 */
export function buildImageObject({
    imageId,
    width,
    height,
    pixelData,
    numberOfComponents,
    voxelManagerFactory,
}) {
    if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
        throw new Error(`A web image needs positive integer dimensions, got ${width}x${height}.`);
    }
    if (numberOfComponents !== 1 && numberOfComponents !== 3) {
        throw new Error(
            `numberOfComponents must be 1 (greyscale) or 3 (RGB), got ${numberOfComponents}. ` +
                'Four would leave vtk.js rendering the alpha channel as a fourth band.'
        );
    }
    const expected = width * height * numberOfComponents;
    if (pixelData?.length !== expected) {
        throw new Error(
            `pixelData is ${pixelData?.length} values; ${width}x${height} at ` +
                `${numberOfComponents} components needs ${expected}. A short buffer renders ` +
                'as a partly black image rather than as an error.'
        );
    }

    const color = numberOfComponents > 1;
    const voxelManager = voxelManagerFactory({
        scalarData: pixelData,
        width,
        height,
        numberOfComponents,
    });

    return {
        imageId,
        rows: height,
        columns: width,
        height,
        width,
        color,
        rgba: false,
        numberOfComponents,
        photometricInterpretation: color ? 'RGB' : 'MONOCHROME2',
        minPixelValue: 0,
        maxPixelValue: MAX_STORED_VALUE,
        // No rescale: a photograph's stored values are display values. Reporting a slope
        // and intercept here would invite a caller to treat them as modality units.
        slope: 1,
        intercept: 0,
        // The full 8-bit range, which is the only defensible opening window for an image
        // with no modality. `grid/voi.js` is not reused: it pushes a modality preset
        // through a residual NIfTI rescale LUT, and a PNG has neither.
        windowCenter: (MAX_STORED_VALUE + 1) / 2,
        windowWidth: MAX_STORED_VALUE + 1,
        voiLUTFunction: 'LINEAR',
        invert: false,
        // `null`, never `undefined` -- see the header. This is what says "no scale is
        // known", and it is what the metadata provider's omitted `pixelSpacing` agrees
        // with.
        rowPixelSpacing: null,
        columnPixelSpacing: null,
        sizeInBytes: pixelData.byteLength,
        voxelManager,
        getPixelData: () => voxelManager.getScalarData(),
        getCanvas: () => undefined,
    };
}

/**
 * RGBA from a canvas to the interleaved buffer the image object wants.
 *
 * Greyscale detection is a real read of the pixels rather than a guess from the file
 * extension: a PNG can be either, and an X-ray stored as RGB triples renders identically
 * but costs three times the memory and takes the colour path, which disables the window
 * tool. Sampling would be wrong -- one coloured annotation burned into a corner makes the
 * image colour -- so this checks every pixel and stops at the first that disagrees.
 *
 * @param {Uint8ClampedArray|Uint8Array} rgba
 * @param {number} pixelCount
 * @returns {{pixelData: Uint8Array, numberOfComponents: number}}
 */
export function repackRgba(rgba, pixelCount) {
    if (rgba.length !== pixelCount * 4) {
        throw new Error(`Expected ${pixelCount * 4} RGBA values, got ${rgba.length}.`);
    }

    let greyscale = true;
    for (let index = 0; index < rgba.length; index += 4) {
        if (rgba[index] !== rgba[index + 1] || rgba[index] !== rgba[index + 2]) {
            greyscale = false;
            break;
        }
    }

    if (greyscale) {
        const out = new Uint8Array(pixelCount);
        for (let pixel = 0; pixel < pixelCount; pixel += 1) {
            out[pixel] = rgba[pixel * 4];
        }
        return { pixelData: out, numberOfComponents: 1 };
    }

    const out = new Uint8Array(pixelCount * 3);
    for (let pixel = 0; pixel < pixelCount; pixel += 1) {
        out[pixel * 3] = rgba[pixel * 4];
        out[pixel * 3 + 1] = rgba[pixel * 4 + 1];
        out[pixel * 3 + 2] = rgba[pixel * 4 + 2];
    }
    return { pixelData: out, numberOfComponents: 3 };
}

/**
 * The loader function to hand `imageLoader.registerImageLoader`.
 *
 * Returns `{promise, cancelFn}`, which is the contract in
 * `@cornerstonejs/core/types/ImageLoaderFn`. `cancelFn` aborts the fetch: a user
 * scrolling a stack faster than the network can keep up would otherwise leave every
 * skipped image decoding.
 *
 * Everything Cornerstone-shaped is injected, so the module imports nothing from the
 * viewer and stays unit-testable without a DOM.
 *
 * @param {object} deps
 * @param {Function} deps.voxelManagerFactory `VoxelManager.createImageVoxelManager`.
 * @param {Function} [deps.fetchImpl] defaults to `globalThis.fetch`.
 * @param {Function} [deps.decodeImpl] `(blob) => {width, height, rgba}`.
 * @returns {Function} `(imageId) => ({promise, cancelFn})`
 */
export function createWebImageLoader({ voxelManagerFactory, fetchImpl, decodeImpl }) {
    const doFetch = fetchImpl ?? ((...args) => globalThis.fetch(...args));
    const decode = decodeImpl ?? decodeWithCanvas;

    return function loadWebImage(imageId) {
        const controller = new AbortController();
        const url = parseWebImageId(imageId);

        const promise = (async () => {
            // Same-origin by default, which is what carries the session cookie under
            // SESSION_COOKIE_SAMESITE = "Strict". Reads need no CSRF token.
            const response = await doFetch(url, {
                credentials: 'same-origin',
                signal: controller.signal,
            });
            if (!response.ok) {
                throw new Error(`HTTP ${response.status} loading ${url}`);
            }
            const { width, height, rgba } = await decode(await response.blob());
            const { pixelData, numberOfComponents } = repackRgba(rgba, width * height);
            return buildImageObject({
                imageId,
                width,
                height,
                pixelData,
                numberOfComponents,
                voxelManagerFactory,
            });
        })();

        return { promise, cancelFn: () => controller.abort() };
    };
}

/**
 * Decode a blob to RGBA via `createImageBitmap` and an `OffscreenCanvas`.
 *
 * Split out so the loader can be tested without either. `OffscreenCanvas` keeps the
 * decode off the DOM; both are available in every browser decision #13 admits.
 *
 * @param {Blob} blob
 * @returns {Promise<{width: number, height: number, rgba: Uint8ClampedArray}>}
 */
async function decodeWithCanvas(blob) {
    const bitmap = await globalThis.createImageBitmap(blob);
    try {
        const canvas = new globalThis.OffscreenCanvas(bitmap.width, bitmap.height);
        const context = canvas.getContext('2d', { willReadFrequently: true });
        context.drawImage(bitmap, 0, 0);
        const { data } = context.getImageData(0, 0, bitmap.width, bitmap.height);
        return { width: bitmap.width, height: bitmap.height, rgba: data };
    } finally {
        bitmap.close?.();
    }
}
