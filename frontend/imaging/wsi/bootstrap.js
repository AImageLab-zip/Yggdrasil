/**
 * Bootstrap the WSI viewer when #urologyWsiData exists on the page.
 */

import { createWsiViewport } from './wsiViewport.js';
import { wireWsiControls } from './wsiControls.js';

export async function bootstrapWsiViewer({
    dataElementId = 'urologyWsiData',
    stageElementId = 'urologyWsiStage',
    controlsPrefix = 'urologyWsi',
} = {}) {
    const dataEl = document.getElementById(dataElementId);
    if (!dataEl) {
        return null;
    }

    let payload;
    try {
        payload = JSON.parse(dataEl.textContent || '{}');
    } catch (err) {
        console.error('Failed to parse WSI viewer JSON payload:', err);
        return null;
    }

    const stageEl = document.getElementById(stageElementId);
    if (!stageEl) {
        console.warn(`WSI stage element #${stageElementId} not found.`);
        return null;
    }

    const {
        patientId,
        fileId,
        metadata,
        revision = 0,
        annotations = [],
        csrfToken = '',
        namespace = 'urology',
    } = payload;

    if (!fileId) {
        console.warn('WSI viewer payload has no fileId.');
        return null;
    }

    let slideMetadata = metadata;
    if (!slideMetadata || !slideMetadata.levels) {
        try {
            const res = await fetch(`/${namespace}/api/wsi/${fileId}/metadata/`, { credentials: 'same-origin' });
            if (res.ok) {
                slideMetadata = await res.json();
            }
        } catch (e) {
            console.error('Failed to fetch WSI metadata:', e);
        }
    }

    if (!slideMetadata) {
        slideMetadata = {
            width: 2000,
            height: 2000,
            tile_size: 256,
            mpp_x: 0.25,
            mpp_y: 0.25,
            levels: [{ level: 0, width: 2000, height: 2000, downsample: 1.0, cols: 8, rows: 8 }],
        };
    }

    const viewport = createWsiViewport({
        element: stageEl,
        metadata: slideMetadata,
        fileId,
        namespace,
        onSegmentationLoaded: (data) => {
            const toggleSegBtn = document.getElementById(`${controlsPrefix}ToggleSegBtn`);
            if (toggleSegBtn && data && data.hasSegmentation) {
                toggleSegBtn.style.display = 'inline-flex';
                const labelSpan = toggleSegBtn.querySelector('span');
                if (labelSpan && data.featureCount) {
                    labelSpan.textContent = `Seg (${data.featureCount})`;
                }
            }
        },
    });

    if (Array.isArray(annotations) && annotations.length) {
        viewport.setAnnotations(annotations);
    }

    wireWsiControls({
        viewport,
        metadata: slideMetadata,
        fileId,
        patientId,
        initialRevision: revision,
        csrfToken,
        namespace,
        controlsPrefix,
    });

    return { viewport };
}
