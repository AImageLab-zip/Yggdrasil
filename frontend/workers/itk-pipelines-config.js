/**
 * Point itk-wasm's morphological-contour-interpolation at our vendored pipelines.
 *
 * Imported first by `interpolation-worker.js`, and only from there: the worker
 * dynamically imports the package itself (`peerImport('@itk-wasm/...')` in
 * `@cornerstonejs/labelmap-interpolation/dist/esm/workers/interpolationWorker.js`),
 * so the module instance that matters is the worker's own -- setting this on the main
 * thread would have no effect on it.
 *
 * The URL is derived from `import.meta.url` rather than written literally, because
 * the emitted tree lives under a version-stamped directory
 * (`static/vendor/cornerstone/<build>/`) whose name is a hash of this very source.
 * Hardcoding it would be circular. After bundling, `import.meta.url` here is the
 * worker's own output URL, `<build>/app/workers/interpolationWorker.js`, so the
 * pipelines resolve two levels up.
 */

import { setPipelinesBaseUrl } from '@itk-wasm/morphological-contour-interpolation';

setPipelinesBaseUrl(new URL('../../itk/pipelines', import.meta.url).href);
