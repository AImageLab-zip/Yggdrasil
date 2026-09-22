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
            } else {
                const errBody = await res.json().catch(() => ({}));
                console.error('Failed to fetch WSI metadata:', res.status, errBody);
                stageEl.innerHTML = `
                    <div class="d-flex flex-column align-items-center justify-content-center h-100 p-4 text-center text-muted" style="min-height: 400px; background: #0f172a;">
                        <i class="fas fa-triangle-exclamation fa-3x text-warning mb-3"></i>
                        <h5 class="text-white">Unable to load slide image</h5>
                        <p class="small text-muted mb-0" style="max-width: 480px;">${errBody.error || 'The slide file could not be parsed or resolution levels are unavailable.'}</p>
                    </div>
                `;
                return null;
            }
        } catch (e) {
            console.error('Failed to fetch WSI metadata:', e);
            stageEl.innerHTML = `
                <div class="d-flex flex-column align-items-center justify-content-center h-100 p-4 text-center text-muted" style="min-height: 400px; background: #0f172a;">
                    <i class="fas fa-circle-exclamation fa-3x text-danger mb-3"></i>
                    <h5 class="text-white">Connection or parsing error</h5>
                    <p class="small text-muted mb-0" style="max-width: 480px;">Failed to communicate with the slide rendering service.</p>
                </div>
            `;
            return null;
        }
    }

    if (!slideMetadata || !slideMetadata.levels || !slideMetadata.levels.length) {
        stageEl.innerHTML = `
            <div class="d-flex flex-column align-items-center justify-content-center h-100 p-4 text-center text-muted" style="min-height: 400px; background: #0f172a;">
                <i class="fas fa-triangle-exclamation fa-3x text-warning mb-3"></i>
                <h5 class="text-white">Invalid slide structure</h5>
                <p class="small text-muted mb-0" style="max-width: 480px;">The slide does not contain multi-resolution pyramidal levels.</p>
            </div>
        `;
        return null;
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
