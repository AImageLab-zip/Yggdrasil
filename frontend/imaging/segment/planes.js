/**
 * Label planes: the bytes a labelmap becomes on the wire.
 *
 * A **plane** is one label's mask for one frame -- a `Uint8Array` of `width * height`
 * values, 0 or 1. Labels are independent planes, which is what lets two of them cover the
 * same pixel. On the wire a plane is `base64(gzip(plane))`: the browser has
 * `CompressionStream` and no zip library, so the *server* owns the archive format and the
 * client only ever produces and consumes this.
 *
 * Measured at 1080p with four overlapping organ masks: 8.3 MB raw, ~17 KB gzipped, ~50 ms.
 * Nothing cleverer (bit-packing, run-length) earns its code at that size.
 *
 * Streams and `btoa` are injected so the codec runs under `node --test` without a browser.
 */

/** Largest slice handed to `String.fromCharCode` at once; more overflows the argument stack. */
const CHUNK = 0x8000;

/** @param {Uint8Array} bytes */
export function bytesToBase64(bytes, btoaImpl = globalThis.btoa) {
    let binary = '';
    for (let offset = 0; offset < bytes.length; offset += CHUNK) {
        binary += String.fromCharCode(...bytes.subarray(offset, offset + CHUNK));
    }
    return btoaImpl(binary);
}

/** @returns {Uint8Array} */
export function base64ToBytes(text, atobImpl = globalThis.atob) {
    const binary = atobImpl(text);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
        bytes[index] = binary.charCodeAt(index);
    }
    return bytes;
}

async function pipe(bytes, transform, { BlobImpl, ResponseImpl }) {
    const stream = new BlobImpl([bytes]).stream().pipeThrough(transform);
    return new Uint8Array(await new ResponseImpl(stream).arrayBuffer());
}

const defaults = () => ({
    BlobImpl: globalThis.Blob,
    ResponseImpl: globalThis.Response,
    CompressionStreamImpl: globalThis.CompressionStream,
    DecompressionStreamImpl: globalThis.DecompressionStream,
});

/**
 * A plane's wire encoding.
 *
 * @param {Uint8Array} plane 0/1 values.
 * @returns {Promise<string>}
 */
export async function encodePlane(plane, deps = {}) {
    const impl = { ...defaults(), ...deps };
    const gzipped = await pipe(plane, new impl.CompressionStreamImpl('gzip'), impl);
    return bytesToBase64(gzipped, impl.btoaImpl);
}

/**
 * The plane a wire string holds, checked against the size the caller expects.
 *
 * @returns {Promise<Uint8Array>}
 * @throws {Error} if the inflated length is not `width * height` or a value is above 1.
 */
export async function decodePlane(text, width, height, deps = {}) {
    const impl = { ...defaults(), ...deps };
    const plane = await pipe(
        base64ToBytes(text, impl.atobImpl),
        new impl.DecompressionStreamImpl('gzip'),
        impl
    );
    if (plane.length !== width * height) {
        throw new Error(
            `a plane holds ${plane.length} values; ${width}x${height} needs ${width * height}`
        );
    }
    return plane;
}

/** True when no pixel is set. An empty plane is not sent: a frame of them is a deletion. */
export function isEmptyPlane(plane) {
    for (let index = 0; index < plane.length; index += 1) {
        if (plane[index] !== 0) {
            return false;
        }
    }
    return true;
}

/**
 * A Cornerstone labelmap's pixels as a 0/1 plane.
 *
 * Any non-zero value counts: the labelmap holds the label's segment index, but a plane
 * only records presence, so the mapping from index to label stays out of the stored bytes.
 */
export function planeFromLabelmap(pixels) {
    const plane = new Uint8Array(pixels.length);
    for (let index = 0; index < pixels.length; index += 1) {
        plane[index] = pixels[index] === 0 ? 0 : 1;
    }
    return plane;
}

/**
 * Write a 0/1 plane into a labelmap's pixel buffer, as `segmentIndex` where set.
 *
 * Mutates `pixels` in place -- it is Cornerstone's own buffer, and replacing it would
 * detach the image from the voxel manager that owns it.
 */
export function writePlaneToLabelmap(pixels, plane, segmentIndex = 1) {
    if (pixels.length !== plane.length) {
        throw new Error(`labelmap has ${pixels.length} pixels, plane has ${plane.length}`);
    }
    for (let index = 0; index < pixels.length; index += 1) {
        pixels[index] = plane[index] === 0 ? 0 : segmentIndex;
    }
}
