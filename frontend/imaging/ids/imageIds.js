/**
 * Build the URLs and imageIds Cornerstone's NIfTI loader will accept.
 *
 * Pure: no Cornerstone import, no DOM, no fetch. Every rule below is a real defect in
 * `@cornerstonejs/nifti-volume-loader@5.8.2` that fails far away from its cause, which
 * is why this is a module with tests rather than a line of string concatenation at
 * each call site.
 *
 * Finding F3 of docs/cornerstone-roadmap.md, in
 * `createNiftiImageIdsAndCacheMetadata.js`:
 *
 *   1. `const _url = new URL(url)` -- one argument, so it **throws** on a relative
 *      path. Every serve URL in this codebase is relative
 *      (`/maxillo/api/processing/files/serve/123/`). Hence {@link toAbsoluteUrl}.
 *   2. `const isCompressed = _url.pathname.endsWith('.gz')` -- the *pathname*, so a
 *      query string is invisible to it and `?ext=.gz` cannot rescue a URL that has no
 *      suffix. Hence the filename-suffixed serve route and {@link serveFilePath}.
 *
 * And one the roadmap does not record, found while writing this:
 *
 *   3. `const imageId = \`nifti:${niftiURL}?frame=${i}\`` (line 174) appends `?frame=`
 *      with a literal `?`, unconditionally. A URL that already carries a query string
 *      therefore becomes `nifti:/path?file_key=x?frame=0` -- two `?`, so `frame`
 *      parses as part of the `file_key` value and every slice resolves to frame 0.
 *      {@link assertLoaderSafeUrl} refuses such a URL instead of building it.
 *
 * (3) matters concretely: a `cbct_processed` row with `file_hash == 'multi-file'` is
 * addressed as `?file_key=segmentation_nifti` (`maxillo/api_views/files.py:45-65`), and
 * that is a volume Phase 3 has to display. Phase 1 draws the boundary loudly rather
 * than changing the serve contract to suit the viewer; see the roadmap's F14.
 */

/** The loader's own scheme prefix (`nifti-volume-loader/constants/niftiLoaderScheme`). */
export const NIFTI_SCHEME = 'nifti';

/** Namespaces that expose the file-serving API. `api` is the global, unprefixed one. */
export const SERVE_NAMESPACES = Object.freeze(['api', 'maxillo', 'brain', 'laparoscopy']);

/**
 * Path of the filename-suffixed serve route, matching the Django `api_serve_file_named`
 * route registered in `maxillo/app_urls.py`, `brain/app_urls.py` and
 * `maxillo/api_urls.py`.
 *
 * @param {object} options
 * @param {number|string} options.fileId `FileRegistry.id`.
 * @param {string} options.filename final path segment; must carry the real extension.
 * @param {string} [options.namespace] one of {@link SERVE_NAMESPACES}.
 * @returns {string} e.g. `/maxillo/api/processing/files/serve/123/volume.nii.gz`
 */
export function serveFilePath({ fileId, filename, namespace = 'api' }) {
    if (!SERVE_NAMESPACES.includes(namespace)) {
        throw new Error(`Unknown serve namespace '${namespace}'.`);
    }
    if (!Number.isInteger(Number(fileId)) || Number(fileId) <= 0) {
        throw new Error(`fileId must be a positive integer, got ${JSON.stringify(fileId)}.`);
    }
    assertServableFilename(filename);

    const prefix = namespace === 'api' ? '' : `/${namespace}`;
    // No trailing slash: the suffix has to be the last thing in the path, or rule (2)
    // above goes false for a gzipped volume.
    return `${prefix}/api/processing/files/serve/${Number(fileId)}/${filename}`;
}

/**
 * Reject a filename that would defeat the route or the loader's `.gz` detection.
 *
 * @param {string} filename
 */
export function assertServableFilename(filename) {
    if (typeof filename !== 'string' || filename.length === 0) {
        throw new Error('filename is required: it is what carries the extension the loader reads.');
    }
    if (filename.includes('/') || filename.includes('\\')) {
        throw new Error(
            `filename must be a single path segment, got '${filename}'. ` +
                "Django's <str:> converter would not match it."
        );
    }
    if (filename.includes('?') || filename.includes('#')) {
        throw new Error(`filename must not contain a query or fragment marker, got '${filename}'.`);
    }
}

/**
 * Make a possibly-relative URL absolute, so `new URL(url)` cannot throw (rule 1).
 *
 * @param {string} url absolute or root-relative.
 * @param {object} [options]
 * @param {string} [options.origin] defaults to the document origin.
 * @returns {string}
 */
export function toAbsoluteUrl(url, { origin } = {}) {
    if (typeof url !== 'string' || url.length === 0) {
        throw new Error('url is required.');
    }
    const base = origin ?? globalThis.location?.origin;
    if (!base) {
        throw new Error('No origin available: pass options.origin explicitly.');
    }
    // Already absolute: normalise but keep it. Relative: resolve against the origin.
    return new URL(url, base).href;
}

/**
 * Refuse a URL the loader would mangle.
 *
 * @param {string} absoluteUrl
 * @returns {string} the same URL, for chaining.
 */
export function assertLoaderSafeUrl(absoluteUrl) {
    const parsed = new URL(absoluteUrl);

    if (parsed.search) {
        // Rule (3): the loader appends `?frame=N` with a literal `?`.
        throw new Error(
            `Cornerstone's NIfTI loader appends '?frame=N' unconditionally, so a URL ` +
                `that already has a query string ('${parsed.search}') produces a ` +
                `malformed imageId in which every slice resolves to frame 0. ` +
                `Address the artifact through its path instead.`
        );
    }
    if (parsed.hash) {
        throw new Error(`A fragment ('${parsed.hash}') has no meaning in an imageId.`);
    }
    return absoluteUrl;
}

/**
 * Build the loader URL for one volume: absolute, suffixed, query-free.
 *
 * @param {object} options passed to {@link serveFilePath}, plus `origin`.
 * @returns {string}
 */
export function volumeUrl({ fileId, filename, namespace = 'api', origin } = {}) {
    const path = serveFilePath({ fileId, filename, namespace });
    return assertLoaderSafeUrl(toAbsoluteUrl(path, { origin }));
}

/**
 * Build the `nifti:`-prefixed value `createNiftiImageIdsAndCacheMetadata` expects.
 *
 * Note what this is *not*: the per-slice imageIds. Those are `nifti:<url>?frame=N`, and
 * the loader mints them itself. Cornerstone runtime ids are session-scoped and are never
 * persisted (the governing rule in docs/cornerstone-roadmap.md), so nothing downstream
 * may treat the return value as an identity.
 *
 * @param {object} options as {@link volumeUrl}.
 * @returns {string} e.g. `nifti:https://host/maxillo/api/.../123/volume.nii.gz`
 */
export function niftiVolumeImageId(options) {
    return `${NIFTI_SCHEME}:${volumeUrl(options)}`;
}

/**
 * Upgrade a legacy trailing-slash serve URL to the filename-suffixed form.
 *
 * `static/js/viewer_grid.js:1069` builds the old shape today. Phase 3 deletes that
 * file, but during migration both shapes exist and only the suffixed one works.
 *
 * @param {string} url e.g. `/maxillo/api/processing/files/serve/123/`
 * @param {string} filename the segment to append.
 * @returns {string}
 */
export function upgradeLegacyServeUrl(url, filename) {
    assertServableFilename(filename);
    if (typeof url !== 'string' || !url.includes('/processing/files/serve/')) {
        throw new Error(`Not a file-serve URL: '${url}'.`);
    }
    const [path, query] = url.split('?', 2);
    if (query) {
        // Deliberately not silently dropped -- a `file_key` names a *different* file.
        throw new Error(
            `Cannot upgrade '${url}': its query string ('${query}') would be lost, and ` +
                `the loader cannot accept it either (see assertLoaderSafeUrl).`
        );
    }
    const base = path.endsWith('/') ? path : `${path}/`;
    return `${base}${filename}`;
}
