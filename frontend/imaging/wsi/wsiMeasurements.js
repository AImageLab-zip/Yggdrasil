/**
 * WSI measurements adapter, protocol compliance, and save request building.
 *
 * Coordinates are mapped to Level 0 slide pixel space (`image_pixel`), which is
 * independent of viewer zoom level and camera pan.
 *
 * Microscopic calibration (MPP: microns per pixel) is recorded in the resource descriptor
 * so lengths and areas are physically meaningful in micrometers (µm) or millimeters (mm).
 */

import {
    assertSavable,
    interpretSaveResponse,
    measurementAnnotations,
    MAX_ANNOTATIONS,
    MEASUREMENT_TOOLS,
} from '../annotations/protocol.js';

export const WSI_COORDINATE_SYSTEM = 'image_pixel';

/** Point markers, which the server accepts via its own LABEL_TOOLS set. */
export const WSI_LABEL_TOOLS = Object.freeze(['Label']);

/**
 * Tools whose annotations are persisted for WSI.
 *
 * Derived from the shared list rather than written out again: `protocol.js` says
 * a second copy is "a second thing to keep in step with the Python", and
 * `frontend/tests/measurements.test.js` pins that list against
 * `annotations/adapters/cornerstone.py`. A hand-written subset here was pinned to
 * nothing and had already drifted. The extra names cost nothing -- this viewport
 * creates no Height, Angle or Probe annotations to filter.
 */
export const WSI_MEASUREMENT_TOOLS = Object.freeze([
    ...MEASUREMENT_TOOLS,
    ...WSI_LABEL_TOOLS,
]);

/**
 * Build a WSI resource descriptor recording slide dimensions and optical calibration.
 *
 * @param {object} options
 * @param {object} options.metadata slide metadata from backend
 * @returns {object}
 */
export function wsiDescriptor({ metadata }) {
    return {
        shape: [metadata?.height || 0, metadata?.width || 0],
        mpp_x: metadata?.mpp_x || 0.25,
        mpp_y: metadata?.mpp_y || 0.25,
        tile_size: metadata?.tile_size || 256,
        levels: metadata?.levels?.length || 1,
        recorded_by: 'wsi-viewer',
    };
}

/**
 * Build the save payload for `/urology/api/patients/<id>/measurements/`.
 *
 * @param {object} options
 * @param {number} options.fileId
 * @param {object[]} options.annotations
 * @param {object} options.metadata
 * @param {number} options.expectedRevision
 * @returns {object}
 */
export function buildWsiSaveRequest({ fileId, annotations, metadata, expectedRevision }) {
    if (!Number.isInteger(fileId) || fileId <= 0) {
        throw new Error(`fileId must be a positive integer, got ${JSON.stringify(fileId)}.`);
    }
    assertSavable(annotations, expectedRevision);

    return {
        fileId,
        expectedRevision,
        coordinateSystem: WSI_COORDINATE_SYSTEM,
        volumeDescriptor: wsiDescriptor({ metadata }),
        annotations: filterWsiAnnotations(annotations),
    };
}

/**
 * Keep only the annotations supported on WSI.
 *
 * @param {object[]} annotations
 * @returns {object[]}
 */
export function filterWsiAnnotations(annotations) {
    if (!Array.isArray(annotations)) return [];
    return annotations.filter((entry) => WSI_MEASUREMENT_TOOLS.includes(entry?.metadata?.toolName));
}

export {
    assertSavable,
    interpretSaveResponse,
    measurementAnnotations,
    MAX_ANNOTATIONS,
};
