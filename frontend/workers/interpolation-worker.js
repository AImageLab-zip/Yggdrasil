/**
 * Worker entry: labelmap interpolation.
 *
 * A wrapper rather than the vendor worker used directly, for one reason: the vendored
 * itk-wasm pipelines URL has to be set inside this worker's own module graph (see
 * `itk-pipelines-config.js`). Import order is significant and is why the config is a
 * separate module -- ES modules evaluate imports in source order.
 *
 * The vendor worker is reached by filesystem path because
 * `@cornerstonejs/labelmap-interpolation`'s `exports` map publishes only `.` and
 * `./version`, so `@cornerstonejs/labelmap-interpolation/dist/esm/workers/...` is not
 * a resolvable specifier. esbuild resolves a relative path directly and bypasses the
 * exports map, which is exactly what is wanted here.
 */

import './itk-pipelines-config.js';
import '../../node_modules/@cornerstonejs/labelmap-interpolation/dist/esm/workers/interpolationWorker.js';
