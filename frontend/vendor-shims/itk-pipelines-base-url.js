/**
 * Build-time replacement for
 * `@itk-wasm/morphological-contour-interpolation/dist/pipelines-base-url.js`.
 *
 * Finding F5 of docs/cornerstone-roadmap.md: the vendor module's default is
 *
 *     let defaultPipelinesBaseUrl =
 *         `https://cdn.jsdelivr.net/npm/@itk-wasm/morphological-contour-interpolation@${version}/dist/pipelines`;
 *
 * so shipping `labelmap-interpolation` without calling `setPipelinesBaseUrl()` silently
 * reintroduces a runtime CDN -- against the GDPR rationale stated at
 * `templates/base.html:42-44`.
 *
 * Calling the setter would be enough to stop the *fetch*, but not enough to stop the
 * *string*: the jsdelivr literal would still be present in the emitted bundle, which
 * would force `scripts/check_bundle_assets.mjs` to allowlist a CDN host and thereby
 * weaken the guard the roadmap's risk #4 depends on. Aliasing the module away instead
 * makes the fallback unreachable by construction, and keeps the no-CDN assertion
 * absolute: *no* emitted file may name a CDN.
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
