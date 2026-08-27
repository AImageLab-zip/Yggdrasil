/**
 * Build-time replacement for
 * `@itk-wasm/morphological-contour-interpolation/dist/pipelines-base-url.js`.
 *
 * Finding F5 of docs/cornerstone-roadmap.md: the vendor module's default is
 *
 *     let defaultPipelinesBaseUrl =
 *         `https://cdn.jsdelivr.net/npm/@itk-wasm/morphological-contour-interpolation@${version}/dist/pipelines`;
 *
 * so shipping `labelmap-interpolation` without calling `setPipelinesBaseUrl()` fetches
 * the pipelines from jsdelivr at runtime.
 *
 * That is **not** a policy problem -- CDNs are allowed here, see `CONTRIBUTING.md`. It is
 * a correctness problem. Those are wasm blobs whose ABI is pinned to the package version,
 * the vendored copies under `itk/pipelines` are the ones this build was tested against,
 * and a viewer that silently falls back to a URL built from a version string is a viewer
 * that can start failing without a commit to bisect.
 *
 * Calling the setter would be enough to stop the *fetch*, but the jsdelivr literal would
 * still sit in the emitted bundle, leaving a live fallback path one refactor away from
 * being taken. Aliasing the module away makes it unreachable by construction, which is
 * also what makes `scripts/check_bundle_assets.mjs` noting a CDN host in the bundle a
 * useful signal: it means this alias stopped applying.
 *
 * `frontend/workers/itk-pipelines-config.js` is what actually sets the URL, from
 * `import.meta.url` inside the worker, so it survives the version-stamped output
 * directory without the build hash ever appearing in source.
 */

let pipelinesBaseUrl;

export function setPipelinesBaseUrl(baseUrl) {
    pipelinesBaseUrl = baseUrl;
}

export function getPipelinesBaseUrl() {
    if (typeof pipelinesBaseUrl === 'undefined') {
        // Loud, not silent: the vendor default we removed would have quietly gone to
        // a third-party CDN with patient-derived work in flight.
        throw new Error(
            'Yggdrasil: itk-wasm pipelines base URL was never set. ' +
                'frontend/workers/itk-pipelines-config.js must be imported before any ' +
                'morphological-contour-interpolation pipeline runs.'
        );
    }
    return pipelinesBaseUrl;
}
