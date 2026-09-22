/**
 * Entry point: Digital Pathology Whole Slide Image (WSI) viewer.
 *
 * Multi-resolution pyramidal tile rendering for digital pathology slides, with
 * measurements saved through the shared annotation protocol.
 *
 * Deliberately no Cornerstone image loader. One was registered here and never
 * reached: the page runs from the two bootstrapWsiViewer() calls below, which go
 * straight to createWsiViewport and fetch tiles themselves. Registering it also
 * pulled @cornerstonejs/core -- and with it the 4 MB shared chunk -- into a
 * bundle that never initialised Cornerstone. Cornerstone 5.8.2 as vendored has
 * no whole-slide viewport; see docs/wsi_viewer_process.md.
 */

import { createWsiViewport } from '../imaging/wsi/wsiViewport.js';
import { bootstrapWsiViewer } from '../imaging/wsi/bootstrap.js';
import { WSI_MEASUREMENT_TOOLS } from '../imaging/wsi/wsiMeasurements.js';
import { wsiTileUrl } from '../imaging/wsi/wsiLoader.js';

export const SURFACE = 'wsi-viewer';

/**
 * Auto-start on load if #urologyWsiData or #urologyConfocalData is present.
 */
const startedWsi = bootstrapWsiViewer({
    dataElementId: 'urologyWsiData',
    stageElementId: 'urologyWsiStage',
    controlsPrefix: 'urologyWsi',
}).catch((err) => {
    console.error('The WSI viewer failed to start:', err);
    return null;
});

const startedConfocal = bootstrapWsiViewer({
    dataElementId: 'urologyConfocalData',
    stageElementId: 'urologyConfocalStage',
    controlsPrefix: 'urologyConfocal',
}).catch((err) => {
    console.error('The Confocale viewer failed to start:', err);
    return null;
});

const started = Promise.all([startedWsi, startedConfocal]);

export {
    started,
    startedWsi,
    startedConfocal,
    bootstrapWsiViewer,
    createWsiViewport,
    createWsiViewport as default,
    WSI_MEASUREMENT_TOOLS,
    wsiTileUrl,
};
