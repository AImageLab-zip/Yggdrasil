/**
 * Render-capability detection.
 *
 * Decision #13 of docs/cornerstone-roadmap.md: **WebGL2 is required** and the user
 * gets an explicit unsupported-browser message; **WebGPU is opportunistic**; touch
 * is supported. This module is the single place that decision is expressed, and it
 * is deliberately pure -- it takes the objects it probes as arguments so it can be
 * unit-tested under `node --test` with no DOM.
 *
 * It reports; it does not decide policy or render UI. Callers do that.
 */

/** Returned by {@link detectCapabilities} when WebGL2 is missing. */
export const UNSUPPORTED_MESSAGE =
    'This browser cannot display medical images: WebGL2 is required. ' +
    'Please use an up-to-date version of Firefox, Chrome, Edge or Safari, ' +
    'and check that hardware acceleration is enabled.';

/**
 * Probe a browser-like environment for the rendering features the viewers need.
 *
 * @param {object} [env] injection seam for tests; defaults to the real globals.
 * @param {Document} [env.document]
 * @param {object} [env.navigator]
 * @returns {{webgl2: boolean, webgpu: boolean, supported: boolean, message: string|null}}
 */
export function detectCapabilities(env = {}) {
    const doc = 'document' in env ? env.document : globalThis.document;
    const nav = 'navigator' in env ? env.navigator : globalThis.navigator;

    const webgl2 = hasWebGL2(doc);
    // Presence of the API only. Adapter acquisition is async and a viewport must
    // never block on it, so WebGPU stays strictly opportunistic.
    const webgpu = Boolean(nav && nav.gpu);

    return {
        webgl2,
        webgpu,
        supported: webgl2,
        message: webgl2 ? null : UNSUPPORTED_MESSAGE,
    };
}

function hasWebGL2(doc) {
    if (!doc || typeof doc.createElement !== 'function') {
        return false;
    }
    try {
        const canvas = doc.createElement('canvas');
        if (!canvas || typeof canvas.getContext !== 'function') {
            return false;
        }
        return Boolean(canvas.getContext('webgl2'));
    } catch {
        // Some hardened/headless environments throw rather than returning null.
        return false;
    }
}
