import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
    WEB_IMAGE_SCHEME,
    buildImageObject,
    createWebImageLoader,
    parseWebImageId,
    repackRgba,
    uriSpellings,
    webImageId,
} from '../imaging/loaders/webImageLoader.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..', '..');
const require = createRequire(import.meta.url);

/** A stand-in for `VoxelManager.createImageVoxelManager`. */
function fakeVoxelManager({ scalarData }) {
    return { getScalarData: () => scalarData };
}

// ---------------------------------------------------------------------------
// The scheme, and the reason it lost its hyphen
// ---------------------------------------------------------------------------

test('the scheme is a single token, so both of Cornerstone URI spellings agree', () => {
    // `@cornerstonejs/metadata/utilities/imageIdToURI.js` matches /^[a-zA-Z]+:/ while
    // `tools/utilities/planar/filterAnnotationsForDisplay.js` splits on the first colon.
    // A hyphenated scheme makes the regex miss, so the two disagree on every id --
    // latent in 5.8.2 and one refactor away from restored annotations never drawing.
    const id = `${WEB_IMAGE_SCHEME}:https://host/maxillo/api/processing/files/serve/7/a.jpg`;
    const { byRegex, bySubstring } = uriSpellings(id);
    assert.equal(byRegex, bySubstring);
    assert.equal(byRegex, 'https://host/maxillo/api/processing/files/serve/7/a.jpg');
});

test('the hyphenated scheme the roadmap named would have disagreed', () => {
    // Pinned so the decision is not quietly reverted by someone matching the roadmap
    // text: this is the evidence for the amendment, not a hypothetical.
    const { byRegex, bySubstring } = uriSpellings('ygg-web:https://host/a.jpg');
    assert.notEqual(byRegex, bySubstring);
    assert.equal(byRegex, 'ygg-web:https://host/a.jpg', 'the regex does not match a hyphen');
});

test('the imageId is absolute and query-free', () => {
    const id = webImageId({ fileId: 7, filename: 'a.jpg', namespace: 'maxillo', origin: 'https://h' });
    assert.equal(id, 'yggweb:https://h/maxillo/api/processing/files/serve/7/a.jpg');
    assert.equal(parseWebImageId(id), 'https://h/maxillo/api/processing/files/serve/7/a.jpg');
});

test('a foreign scheme is refused rather than coerced', () => {
    // A loader registered for one scheme being handed another means the registration is
    // wrong; guessing would hide that until a NIfTI arrived here and decoded as a JPEG.
    assert.throws(() => parseWebImageId('nifti:https://h/v.nii.gz'), /not a yggweb:/);
    assert.throws(() => parseWebImageId('yggweb:'), /must carry a URL/);
    assert.throws(() => parseWebImageId(null), /must be a string/);
});

// ---------------------------------------------------------------------------
// buildImageObject -- where the three package findings live
// ---------------------------------------------------------------------------

function greyImage(overrides = {}) {
    return buildImageObject({
        imageId: 'yggweb:https://h/a.jpg',
        width: 2,
        height: 2,
        pixelData: new Uint8Array([0, 64, 128, 255]),
        numberOfComponents: 1,
        voxelManagerFactory: fakeVoxelManager,
        ...overrides,
    });
}

test('pixel spacing is null, never undefined', () => {
    // `StackViewport._checkVTKImageDataMatchesCornerstoneImage` accepts a spacing
    // mismatch only via `image.rowPixelSpacing === null && ySpacing === 1.0`. With
    // `undefined` that test fails and every stack step rebuilds the actor and resets the
    // VOI -- which reads as "the window keeps jumping back", not as a type bug.
    const image = greyImage();
    assert.strictEqual(image.rowPixelSpacing, null);
    assert.strictEqual(image.columnPixelSpacing, null);
    assert.ok('rowPixelSpacing' in image, 'the key must be present and null');
});

test('the image carries its own voxelManager', () => {
    // `loaders/imageLoader.js` ensureVoxelManager fills one in for an image that lacks
    // it and then does `delete image.imageFrame.pixelData`. A hand-built image has no
    // imageFrame, so that is a TypeError -- thrown inside handleImageLoadPromise's
    // .then, which surfaces as a spurious IMAGE_LOAD_FAILED while the promise resolves.
    const image = greyImage();
    assert.ok(image.voxelManager, 'without this, ensureVoxelManager runs and throws');
    assert.deepEqual([...image.getPixelData()], [0, 64, 128, 255]);
});

test('the ensureVoxelManager hazard is real in the shipped package', () => {
    // Asserted against the actual source rather than described in a comment, so a
    // version bump that removes the hazard shows up as a failure here and the
    // workaround can be reconsidered rather than carried forever.
    const { readFileSync } = require('node:fs');
    const source = readFileSync(
        join(REPO, 'node_modules', '@cornerstonejs', 'core', 'dist', 'esm', 'loaders', 'imageLoader.js'),
        'utf8'
    );
    assert.match(source, /if \(!image\.voxelManager\)/);
    assert.match(source, /delete image\.imageFrame\.pixelData/);
});

test('sizeInBytes is the real byte length', () => {
    assert.equal(greyImage().sizeInBytes, 4);
    const rgb = greyImage({
        pixelData: new Uint8Array(12),
        numberOfComponents: 3,
    });
    assert.equal(rgb.sizeInBytes, 12);
});

test('colour images declare three components, not four', () => {
    // `getImageDataMetadata` derives numberOfComponents from the image and
    // `createActorMapper` sets setIndependentComponents(false) above one. Four would
    // leave vtk.js rendering the alpha channel as a fourth band.
    const rgb = greyImage({ pixelData: new Uint8Array(12), numberOfComponents: 3 });
    assert.equal(rgb.numberOfComponents, 3);
    assert.equal(rgb.color, true);
    assert.equal(rgb.rgba, false);
    assert.equal(rgb.photometricInterpretation, 'RGB');
});

test('greyscale declares one component and MONOCHROME2', () => {
    const image = greyImage();
    assert.equal(image.numberOfComponents, 1);
    assert.equal(image.color, false);
    assert.equal(image.photometricInterpretation, 'MONOCHROME2');
});

test('the opening window is the full 8-bit range and nothing is rescaled', () => {
    const image = greyImage();
    assert.equal(image.windowCenter, 128);
    assert.equal(image.windowWidth, 256);
    assert.equal(image.slope, 1);
    assert.equal(image.intercept, 0);
    assert.equal(image.minPixelValue, 0);
    assert.equal(image.maxPixelValue, 255);
});

test('a short or mismatched buffer is refused, not rendered', () => {
    // A short buffer renders as a partly black image rather than as an error, which is
    // the kind of thing somebody reports as "the scan looks wrong" months later.
    assert.throws(() => greyImage({ pixelData: new Uint8Array(3) }), /needs 4/);
    assert.throws(() => greyImage({ numberOfComponents: 4 }), /must be 1 \(greyscale\) or 3/);
    assert.throws(() => greyImage({ width: 0 }), /positive integer dimensions/);
    assert.throws(() => greyImage({ height: 2.5 }), /positive integer dimensions/);
});

// ---------------------------------------------------------------------------
// repackRgba
// ---------------------------------------------------------------------------

test('an all-grey image is detected and stored as one component', () => {
    // Not a guess from the file extension: a PNG can be either, and an X-ray stored as
    // RGB triples renders identically while costing three times the memory and taking
    // the colour path, which disables the window tool.
    const rgba = new Uint8ClampedArray([10, 10, 10, 255, 200, 200, 200, 255]);
    assert.deepEqual(repackRgba(rgba, 2), {
        pixelData: new Uint8Array([10, 200]),
        numberOfComponents: 1,
    });
});

test('one non-grey pixel makes the whole image colour', () => {
    // Sampling would be wrong here: a single coloured annotation burned into a corner
    // makes the image colour, and treating it as grey would drop the annotation's hue.
    const rgba = new Uint8ClampedArray([10, 10, 10, 255, 200, 0, 0, 255]);
    const { pixelData, numberOfComponents } = repackRgba(rgba, 2);
    assert.equal(numberOfComponents, 3);
    assert.deepEqual([...pixelData], [10, 10, 10, 200, 0, 0]);
});

test('the alpha channel is dropped, not carried', () => {
    const rgba = new Uint8ClampedArray([1, 2, 3, 128]);
    assert.deepEqual([...repackRgba(rgba, 1).pixelData], [1, 2, 3]);
});

test('a wrong-length buffer is refused', () => {
    assert.throws(() => repackRgba(new Uint8ClampedArray(5), 2), /Expected 8 RGBA values/);
});

// ---------------------------------------------------------------------------
// The loader function
// ---------------------------------------------------------------------------

function loaderWith({ ok = true, status = 200 } = {}) {
    let aborted = false;
    const fetchImpl = (url, options) => {
        options.signal.addEventListener('abort', () => {
            aborted = true;
        });
        return Promise.resolve({ ok, status, blob: () => Promise.resolve('blob') });
    };
    const decodeImpl = () =>
        Promise.resolve({
            width: 2,
            height: 1,
            rgba: new Uint8ClampedArray([10, 10, 10, 255, 20, 20, 20, 255]),
        });
    const load = createWebImageLoader({
        voxelManagerFactory: fakeVoxelManager,
        fetchImpl,
        decodeImpl,
    });
    return { load, wasAborted: () => aborted };
}

test('the loader returns the {promise, cancelFn} shape Cornerstone expects', async () => {
    const { load } = loaderWith();
    const result = load('yggweb:https://h/a.jpg');
    assert.equal(typeof result.promise.then, 'function');
    assert.equal(typeof result.cancelFn, 'function');

    const image = await result.promise;
    assert.equal(image.imageId, 'yggweb:https://h/a.jpg');
    assert.equal(image.width, 2);
    assert.equal(image.numberOfComponents, 1);
});

test('cancelFn aborts the fetch', async () => {
    // A user scrolling a stack faster than the network can keep up would otherwise leave
    // every skipped image decoding.
    const { load, wasAborted } = loaderWith();
    const result = load('yggweb:https://h/a.jpg');
    result.cancelFn();
    await result.promise.catch(() => {});
    assert.equal(wasAborted(), true);
});

test('a failed response rejects with the status', async () => {
    const { load } = loaderWith({ ok: false, status: 404 });
    await assert.rejects(load('yggweb:https://h/a.jpg').promise, /HTTP 404/);
});

test('the request is same-origin so the session cookie travels', async () => {
    // SESSION_COOKIE_SAMESITE = "Strict"; fetch defaults to same-origin credentials, but
    // it is stated explicitly because a later refactor to `cors` would 403 every image
    // with no obvious cause.
    let seen = null;
    const load = createWebImageLoader({
        voxelManagerFactory: fakeVoxelManager,
        fetchImpl: (url, options) => {
            seen = options;
            return Promise.resolve({ ok: true, blob: () => Promise.resolve('blob') });
        },
        decodeImpl: () =>
            Promise.resolve({ width: 1, height: 1, rgba: new Uint8ClampedArray([1, 1, 1, 255]) }),
    });
    await load('yggweb:https://h/a.jpg').promise;
    assert.equal(seen.credentials, 'same-origin');
});
