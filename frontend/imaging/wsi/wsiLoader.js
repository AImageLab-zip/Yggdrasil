/**
 * Cornerstone3D Image Loader for Digital Pathology WSI pyramidal tiles: `wsi:<tile-url>`.
 *
 * Each multi-resolution TIFF tile is fetched as a 256x256 PNG from the backend tile API:
 * `/urology/api/wsi/<file_id>/tile/<level>/<col>_<row>.png`.
 *
 * Decodes via `createImageBitmap` / `OffscreenCanvas` into 3-component RGB or 1-component greyscale
 * pixel data, compatible with Cornerstone's VoxelManager and texture rendering.
 */

export const WSI_IMAGE_SCHEME = 'wsi';

/** 8-bit digital pathology tiles. */
const MAX_STORED_VALUE = 255;

/**
 * Generate a WSI tile URL.
 *
 * @param {object} options
 * @param {number} options.fileId
 * @param {number} options.level
 * @param {number} options.col
 * @param {number} options.row
 * @param {string} [options.namespace] defaults to 'urology'
 * @returns {string}
 */
export function wsiTileUrl({ fileId, level, col, row, namespace = 'urology' }) {
    return `/${namespace}/api/wsi/${fileId}/tile/${level}/${col}_${row}.jpg`;
}

/**
 * Generate a Cornerstone `wsi:` imageId.
 *
 * @param {object} options
 * @param {number} options.fileId
 * @param {number} options.level
 * @param {number} options.col
 * @param {number} options.row
 * @param {string} [options.namespace]
 * @returns {string}
 */
export function wsiImageId({ fileId, level, col, row, namespace = 'urology' }) {
    const path = wsiTileUrl({ fileId, level, col, row, namespace });
    const origin = typeof window !== 'undefined' && window.location ? window.location.origin : '';
    const fullUrl = origin ? `${origin}${path}` : path;
    return `${WSI_IMAGE_SCHEME}:${fullUrl}`;
}

/**
 * Parse a `wsi:` imageId.
 *
 * @param {string} imageId
 * @returns {string} the URL
 */
export function parseWsiImageId(imageId) {
    if (typeof imageId !== 'string') {
        throw new Error('imageId must be a string.');
    }
    const prefix = `${WSI_IMAGE_SCHEME}:`;
    if (!imageId.startsWith(prefix)) {
        throw new Error(
            `${JSON.stringify(imageId)} is not a ${prefix} imageId. This loader is registered for that scheme only.`
        );
    }
    const url = imageId.slice(prefix.length);
    if (!url) {
        throw new Error('A wsi: imageId must carry a URL.');
    }
    return url;
}

/**
 * Repack RGBA buffer to RGB (3 components) or greyscale (1 component).
 *
 * @param {Uint8ClampedArray|Uint8Array} rgba
 * @param {number} pixelCount
 * @returns {{pixelData: Uint8Array, numberOfComponents: number}}
 */
export function repackTileRgba(rgba, pixelCount) {
    const out = new Uint8Array(pixelCount * 3);
    for (let pixel = 0; pixel < pixelCount; pixel += 1) {
        out[pixel * 3] = rgba[pixel * 4];
        out[pixel * 3 + 1] = rgba[pixel * 4 + 1];
        out[pixel * 3 + 2] = rgba[pixel * 4 + 2];
    }
    return { pixelData: out, numberOfComponents: 3 };
}

/**
 * Assemble a Cornerstone IImage for one tile.
 *
 * @param {object} options
 * @returns {object} Cornerstone IImage
 */
export function buildTileImageObject({
    imageId,
    width,
    height,
    pixelData,
    numberOfComponents = 3,
    voxelManagerFactory,
}) {
    const voxelManager = voxelManagerFactory
        ? voxelManagerFactory({
              scalarData: pixelData,
              width,
              height,
              numberOfComponents,
          })
        : {
              getScalarData: () => pixelData,
              getScalarDataLength: () => pixelData.length,
          };

    return {
        imageId,
        rows: height,
        columns: width,
        height,
        width,
        color: true,
        rgba: false,
        numberOfComponents,
        photometricInterpretation: 'RGB',
        minPixelValue: 0,
        maxPixelValue: MAX_STORED_VALUE,
        slope: 1,
        intercept: 0,
        windowCenter: 128,
        windowWidth: 256,
        voiLUTFunction: 'LINEAR',
        invert: false,
        rowPixelSpacing: null,
        columnPixelSpacing: null,
        sizeInBytes: pixelData.byteLength,
        voxelManager,
        getPixelData: () => (voxelManager.getScalarData ? voxelManager.getScalarData() : pixelData),
        getCanvas: () => undefined,
    };
}

/**
 * Decode a blob to RGBA via createImageBitmap and OffscreenCanvas.
 *
 * @param {Blob} blob
 * @returns {Promise<{width: number, height: number, rgba: Uint8ClampedArray, bitmap: ImageBitmap}>}
 */
async function decodeTileWithCanvas(blob) {
    if (typeof globalThis.createImageBitmap === 'function') {
        const bitmap = await globalThis.createImageBitmap(blob);
        try {
            if (typeof globalThis.OffscreenCanvas === 'function') {
                const canvas = new globalThis.OffscreenCanvas(bitmap.width, bitmap.height);
                const ctx = canvas.getContext('2d', { willReadFrequently: true });
                ctx.drawImage(bitmap, 0, 0);
                const { data } = ctx.getImageData(0, 0, bitmap.width, bitmap.height);
                return { width: bitmap.width, height: bitmap.height, rgba: data, bitmap };
            }
        } catch {
            // Fall through if canvas extraction fails
        }
        return { width: bitmap.width, height: bitmap.height, rgba: new Uint8ClampedArray(bitmap.width * bitmap.height * 4), bitmap };
    }
    throw new Error('createImageBitmap is not supported in this environment.');
}

/**
 * In-memory LRU cache for tile bitmaps to ensure instant rendering.
 */
class TileCache {
    constructor(maxSize = 400) {
        this.maxSize = maxSize;
        this.cache = new Map();
    }

    has(key) {
        return this.cache.has(key);
    }

    get(key) {
        if (!this.cache.has(key)) return null;
        const value = this.cache.get(key);
        this.cache.delete(key);
        this.cache.set(key, value);
        return value;
    }

    set(key, value) {
        if (this.cache.has(key)) {
            this.cache.delete(key);
        } else if (this.cache.size >= this.maxSize) {
            const oldestKey = this.cache.keys().next().value;
            const oldest = this.cache.get(oldestKey);
            oldest?.bitmap?.close?.();
            this.cache.delete(oldestKey);
        }
        this.cache.set(key, value);
    }

    clear() {
        for (const entry of this.cache.values()) {
            entry?.bitmap?.close?.();
        }
        this.cache.clear();
    }
}

export const globalWsiTileCache = new TileCache(1500);

/**
 * Create the WSI image loader for imageLoader.registerImageLoader(WSI_IMAGE_SCHEME, loader).
 *
 * @param {object} deps
 * @param {Function} deps.voxelManagerFactory
 * @param {Function} [deps.fetchImpl]
 * @param {Function} [deps.decodeImpl]
 * @returns {Function} loader: (imageId) => { promise, cancelFn }
 */
export function createWsiImageLoader({
    voxelManagerFactory,
    fetchImpl = globalThis.fetch,
    decodeImpl = decodeTileWithCanvas,
    tileCache = globalWsiTileCache,
} = {}) {
    return function loadWsiImage(imageId) {
        const controller = new AbortController();
        const url = parseWsiImageId(imageId);

        const cached = tileCache.get(imageId);
        if (cached) {
            return {
                promise: Promise.resolve(cached.imageObject),
                cancelFn: () => {},
            };
        }

        const promise = (async () => {
            const response = await fetchImpl(url, {
                credentials: 'same-origin',
                signal: controller.signal,
            });
            if (!response.ok) {
                throw new Error(`HTTP ${response.status} loading tile ${url}`);
            }
            const blob = await response.blob();
            const decoded = await decodeImpl(blob);
            const { width, height, rgba, bitmap } = decoded;
            const { pixelData, numberOfComponents } = repackTileRgba(rgba, width * height);

            const imageObject = buildTileImageObject({
                imageId,
                width,
                height,
                pixelData,
                numberOfComponents,
                voxelManagerFactory,
            });

            // Keep bitmap for fast canvas blitting
            tileCache.set(imageId, { imageObject, bitmap });
            return imageObject;
        })();

        return { promise, cancelFn: () => controller.abort() };
    };
}
