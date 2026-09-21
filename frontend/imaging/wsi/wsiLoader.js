/**
 * Where a WSI tile lives, and the cache of the ones already fetched.
 *
 * Tiles are 256x256 images served by the backend tile API,
 * `/<namespace>/api/wsi/<file_id>/tile/<level>/<col>_<row>.jpg`, and drawn to a
 * canvas by `wsiViewport.js` -- which fetches them itself.
 *
 * This file used to also hold a Cornerstone `wsi:` image loader. It was
 * registered by the entry and never reached, and it duplicated
 * `imaging/loaders/webImageLoader.js` without that module's URL guards. Gone.
 */

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

/** LRU of decoded tiles, keyed by `<fileId>:<level>:<col>_<row>`. */
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
