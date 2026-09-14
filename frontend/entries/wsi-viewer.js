/**
 * Entry point: Digital Pathology Whole Slide Image (WSI) viewer.
 *
 * Provides multi-resolution pyramidal TIFF tiled rendering and durable Cornerstone
 * annotations for digital pathology slides.
 */

import {
    imageLoader,
    utilities as coreUtilities,
} from '@cornerstonejs/core';

import { initImaging } from '../imaging/runtime/init.js';
import {
    WSI_IMAGE_SCHEME,
    createWsiImageLoader,
    wsiTileUrl,
    wsiImageId,
} from '../imaging/wsi/wsiLoader.js';
import { createWsiViewport } from '../imaging/wsi/wsiViewport.js';
import { bootstrapWsiViewer } from '../imaging/wsi/bootstrap.js';
import { WSI_MEASUREMENT_TOOLS } from '../imaging/wsi/wsiMeasurements.js';

export const SURFACE = 'wsi-viewer';

let registered = false;

export async function mountWsiViewer(options) {
    await initImaging();

    if (!registered) {
        imageLoader.registerImageLoader(
            WSI_IMAGE_SCHEME,
            createWsiImageLoader({
                voxelManagerFactory: coreUtilities?.VoxelManager?.createImageVoxelManager,
            })
        );
        registered = true;
    }

    return createWsiViewport(options);
}

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
    mountWsiViewer as default,
    WSI_IMAGE_SCHEME,
    WSI_MEASUREMENT_TOOLS,
    wsiTileUrl,
    wsiImageId,
};
