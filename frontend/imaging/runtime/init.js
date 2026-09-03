/**
 * One-time Cornerstone3D runtime bring-up.
 *
 * Phase 1 of docs/cornerstone-roadmap.md builds and verifies the bundle; it wires no
 * viewer. Nothing calls this yet -- the per-surface entry points export it so the
 * bundle graph, the workers and the wasm assets are all genuinely exercised by
 * `npm run build` and `npm run verify` before Phase 3 depends on them.
 */

import { init as coreInit, Enums as coreEnums } from '@cornerstonejs/core';
import { init as toolsInit } from '@cornerstonejs/tools';

import { RENDERING_CONFIG } from './config.js';
import { detectCapabilities } from './capabilities.js';

let initialized = null;

/**
 * Initialise core + tools exactly once per page.
 *
 * **`addons` is not optional decoration.** `@cornerstonejs/tools` reaches its add-ons
 * only through the config this call installs: `getPolySeg()` (tools `config.js`) returns
 * `null` and warns unless `config.addons.polySeg` is set, and the Surface display path
 * (`tools/displayTools/Surface/surfaceDisplay.js`) asks it before it will convert a
 * labelmap into a surface. So a segmentation rendered perfectly on the slice viewports
 * and produced nothing whatever on the 3D one -- which is exactly how it was reported.
 * Importing the package, and even calling its own `init()`, does not register it; only
 * this does. It is passed in rather than imported here so the surfaces that have no 3D
 * segmentation do not pull the polySeg wasm into their bundles.
 *
 * @param {object} [options]
 * @param {object} [options.env] forwarded to {@link detectCapabilities}, for tests.
 * @param {object} [options.addons] tools add-ons, e.g. `{ polySeg }`.
 * @returns {Promise<{capabilities: object, enums: object}>}
 * @throws {Error} if WebGL2 is unavailable (decision #13 -- required, not degraded).
 */
export function initImaging(options = {}) {
    if (initialized) {
        return initialized;
    }

    initialized = (async () => {
        const capabilities = detectCapabilities(options.env);
        if (!capabilities.supported) {
            // Callers surface `capabilities.message`; we refuse rather than render
            // something a clinician might mistake for the real image.
            throw new Error(capabilities.message);
        }

        await coreInit(RENDERING_CONFIG);
        // `toolsInit` *replaces* the config wholesale, so the addons must go in here;
        // there is no later hook that adds one.
        await toolsInit(options.addons ? { addons: options.addons } : undefined);

        return { capabilities, enums: coreEnums };
    })();

    return initialized;
}

/** Test seam: forget that initImaging() ran. */
export function resetImagingInit() {
    initialized = null;
}
