/**
 * The panoramic's volume supply, after NiiVue.
 *
 * `static/js/modality_viewers/cbct_panorex_editor.js` is Phase 7's to rewrite, and
 * until then it stays exactly as it is. But it reaches into the viewer for its data --
 * three methods on `window.ViewerGrid` and one `viewergridvolumeready` event -- so
 * deleting the viewer without replacing that supply would break panoramic
 * reconstruction for every maxillo patient.
 *
 * It would break it *silently*, which is the reason this module exists rather than a
 * shrug. `maxillo/views/panoramic_warmup.py` batch-generates panoramics by loading
 * patient pages in a hidden frame; `common/export_catalog.py` ships the baked PNGs; and
 * decision #8 requires those exports to keep working. A panorex editor whose
 * `getNativeRawVolumeDescriptor()` returned null would simply never start, and the
 * warm-up page would report a run of zero successes that looked like "nothing to do".
 *
 * **The contract is RAS-ordered voxels.** NiiVue reorients every volume on load, so the
 * array the panoramic has always consumed is permuted and flipped into RAS, and its
 * affine is the reoriented one. Handing over Cornerstone's file-order array under the
 * same interface would transpose every exported panoramic while every test still
 * passed. `imaging/geometry/reorient.js` does that conversion, and its property test --
 * reoriented indices map to the same world points -- is what makes the swap safe.
 *
 * This module is a **compatibility shim with a deletion date**. It reproduces an
 * interface shaped by NiiVue so that Phase 3 can remove NiiVue; Phase 7 replaces the
 * panoramic with a live CPR and deletes both sides. Do not build anything new on it.
 */

import { toRasVolume } from '../geometry/reorient.js';
import { describeGeometry } from '../geometry/orientation.js';
import { volumeUrl } from '../ids/imageIds.js';

/** The event `cbct_panorex_editor.js` waits on before it looks for a volume. */
export const VOLUME_READY_EVENT = 'viewergridvolumeready';

/**
 * Build the descriptor `getNativeRawVolumeDescriptor()` has always returned.
 *
 * Field for field the shape `niivue_viewer.js:690-731` produced, because the consumer
 * is unchanged and every field is read:
 *
 *   - `data` -- the voxel array, **RAS-ordered**, raw stored values (not rescaled).
 *     NiiVue's `volume.img` was raw too; the panoramic applies `slope`/`intercept`
 *     itself, so pre-scaling here would double the rescale.
 *   - `dimensions` -- `{width, height, depth}`, in the reoriented order.
 *   - `affine` -- 4x4 nested array, describing the reoriented data.
 *   - `flipZ` -- `affine[2][2] > 0`, carried over verbatim. It is an odd predicate to
 *     inherit, but the panoramic branches on it and this is not the phase to
 *     re-litigate what it means.
 *
 * @param {object} options
 * @param {ArrayLike<number>} options.scalarData file-order voxels from the volume.
 * @param {object} options.header the parsed NIfTI header.
 * @param {object} options.source `{fileId, fileKey, revision}`.
 * @param {string} [options.fileName]
 * @returns {object|null} null when the volume cannot supply one.
 */
export function nativeRawVolumeDescriptor({ scalarData, header, source, fileName = null }) {
    const geometry = describeGeometry(header);
    const dims = geometry.dimensions;

    if (!scalarData || dims.some((size) => !size) || !geometry.affine) {
        return null;
    }
    if (scalarData.length < dims[0] * dims[1] * dims[2]) {
        // The same guard `getNativeVolumeDescriptor` had: a short array means the load
        // is not finished, and returning it would bake a panoramic from padding.
        return null;
    }

    const ras = toRasVolume({ data: scalarData, dims, affine: geometry.affine });

    return {
        data: ras.data,
        dimensions: { width: ras.dims[0], height: ras.dims[1], depth: ras.dims[2] },
        affine: ras.affine,
        flipZ: Boolean(ras.affine[2] && Number(ras.affine[2][2]) > 0),
        slope: Number(header.scl_slope) || 1,
        intercept: Number(header.scl_inter) || 0,
        datatype: header.datatypeCode,
        fileName,
        source,
        // Not part of the old shape. Recorded so a panoramic baked from a volume whose
        // orientation was *inferred* can be told apart later (finding F2).
        orientationInferred: !geometry.declared,
        permRAS: ras.permRAS,
    };
}

/**
 * Fetch the segmentation volume the panoramic pairs with the CBCT.
 *
 * A near-verbatim port of `viewer_grid.js:332-377`, which never touched NiiVue -- it is
 * a fetch and a cache. Carried over rather than rewritten so that the one thing that
 * changes in this migration is the thing that had to.
 *
 * @param {object} options
 * @param {object} options.panorexSource `djangoData.panorexSource`, possibly null.
 * @param {object} options.segmentationFile the fallback `{id, file_key}`.
 * @param {object} options.cache a mutable object used as the memo.
 * @param {string} [options.namespace]
 * @param {string} [options.origin] injection seam; defaults to the document origin.
 * @param {(url: string) => Promise<Response>} [options.fetchImpl]
 * @returns {Promise<{arrayBuffer: ArrayBuffer, descriptor: object}>}
 */
export async function panorexSegmentationSource({
    panorexSource,
    segmentationFile,
    cache,
    namespace = 'api',
    origin,
    fetchImpl = globalThis.fetch,
}) {
    const descriptor = panorexSource?.segmentationFileId
        ? { id: panorexSource.segmentationFileId, fileKey: panorexSource.segmentationFileKey || 'volume_nifti' }
        : segmentationFile;

    if (!descriptor?.id) {
        throw new Error('No paired panoramic segmentation source is available');
    }

    const fileId = String(descriptor.id);
    const fileKey = descriptor.fileKey || descriptor.file_key || 'volume_nifti';
    const cacheKey = `${fileId}:${fileKey}`;

    if (!cache[cacheKey]) {
        const url = volumeUrl({
            fileId: Number(fileId),
            bundleKey: fileKey,
            filename: descriptor.filename || 'segmentation.nii.gz',
            namespace,
            origin,
        });
        const response = await fetchImpl(url, { credentials: 'same-origin' });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
        cache[cacheKey] = await response.arrayBuffer();
    }

    return {
        arrayBuffer: cache[cacheKey],
        descriptor: { ...(panorexSource || {}), fileId, fileKey, revision: panorexSource?.revision ?? null },
    };
}

/**
 * Install the shim on `window.ViewerGrid` and announce the volume.
 *
 * The global is what `cbct_panorex_editor.js` looks for, so the global is what it gets.
 * Named `installPanoramicBridge` rather than anything suggesting a viewer, so nobody
 * mistakes it for the grid's public interface: it is three methods and an event, and
 * they exist for one consumer.
 *
 * @param {object} options
 * @param {() => object|null} options.getDescriptor supplies the raw volume descriptor.
 * @param {object} options.data the `viewerGridData` payload.
 * @param {object} [options.target] defaults to `globalThis`.
 * @returns {object} the installed shim, for tests.
 */
export function installPanoramicBridge({ getDescriptor, data, target = globalThis }) {
    const segmentationCache = {};

    const bridge = {
        getNativeRawVolumeDescriptor: getDescriptor,
        getPanorexSourceDescriptor: () => data.panorexSource ?? null,
        getPanorexSegmentationSource: () =>
            panorexSegmentationSource({
                panorexSource: data.panorexSource,
                segmentationFile: data.segmentationFile,
                cache: segmentationCache,
                namespace: data.projectNamespace || 'api',
            }),
    };

    // Merged rather than assigned: `patient_detail.js` and the panorex editor both
    // reach for `window.ViewerGrid`, and clobbering an existing object would be a
    // load-order dependency nobody could see.
    target.ViewerGrid = Object.assign(target.ViewerGrid || {}, bridge);
    return bridge;
}

/**
 * Tell the panoramic a volume is available.
 *
 * The detail shape is `viewer_grid.js:1408-1410` verbatim. The editor only checks that
 * the event fired and then calls back in, but a changed payload would still be a
 * changed contract, and this shim exists precisely so the consumer needs no changes.
 *
 * @param {object} detail `{windowIndex, modality, fileId}`.
 * @param {object} [target] defaults to `globalThis`.
 */
export function announceVolumeReady(detail, target = globalThis) {
    target.dispatchEvent(new CustomEvent(VOLUME_READY_EVENT, { detail }));
}
