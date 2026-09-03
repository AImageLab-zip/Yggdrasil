/**
 * Where the panoramic gets its CBCT, now that the shim is gone.
 *
 * ## What this replaces
 *
 * `imaging/grid/panoramicSource.js` existed so Phase 3 could delete NiiVue without
 * breaking panoramic reconstruction: it put three methods and an event on
 * `window.ViewerGrid` because the Konva editor reached for them by name. It was documented
 * as a compatibility shim with a deletion date. This is that date.
 *
 * ## Why no bridge is needed
 *
 * Every bundle entry imports the same shared chunk, so `@cornerstonejs/core`'s `cache` is
 * **one module instance per page**. The volume grid loads the CBCT; this surface finds it
 * in that cache. The two agree on *which* volume not by contract but by construction --
 * both derive the id from the same `#viewerGridData` payload through the same three
 * functions -- so there is no interface between them to get out of step.
 *
 * ## The RAS array, and why it is materialised late
 *
 * The baker consumes **RAS-ordered raw stored values**: NiiVue reoriented every volume on
 * load, so that is the array every existing panoramic was produced from, and handing over
 * Cornerstone's file-order one would transpose every exported strip while every test still
 * passed. `geometry/reorient.js` does the conversion.
 *
 * It is a second full copy of a CBCT, so it is built **once, on the first bake, and
 * released on teardown** -- not on every read. The shim rebuilt it on every call and the
 * editor polled, which is a defect this replacement does not inherit.
 */

import { describeGeometry } from '../geometry/orientation.js';
import { toRasVolume } from '../geometry/reorient.js';

/**
 * The descriptor the baker reads, field for field what `getNativeRawVolumeDescriptor`
 * returned -- because `seg2pano_core.js` is unchanged and reads every field.
 *
 * @param {object} options
 * @param {ArrayLike<number>} options.scalarData file-order voxels.
 * @param {object} options.header the parsed NIfTI header.
 * @returns {object|null} null when the volume cannot supply one, which for a short array
 *   means the load is not finished -- returning it would bake a panoramic from padding.
 */
export function rasDescriptor({ scalarData, header }) {
    const geometry = describeGeometry(header);
    const dims = geometry.dimensions;
    if (!scalarData || dims.some((size) => !size) || !geometry.affine) {
        return null;
    }
    if (scalarData.length < dims[0] * dims[1] * dims[2]) {
        return null;
    }

    const ras = toRasVolume({ data: scalarData, dims, affine: geometry.affine });
    return {
        data: ras.data,
        dimensions: { width: ras.dims[0], height: ras.dims[1], depth: ras.dims[2] },
        affine: ras.affine,
        // Carried over verbatim. It is an odd predicate to inherit, but the baker branches
        // on it and Phase 7 is not the phase to re-litigate what it means.
        flipZ: Boolean(ras.affine[2] && Number(ras.affine[2][2]) > 0),
        // Raw, not rescaled: `bilinearAt` applies these itself, so pre-scaling here would
        // apply the modality LUT twice.
        slope: Number(header.scl_slope) || 1,
        intercept: Number(header.scl_inter) || 0,
        datatype: header.datatypeCode,
        // Not part of the old shape. Recorded so a strip baked from a volume whose
        // orientation was *inferred* can be told apart later (finding F2).
        orientationInferred: !geometry.declared,
        permRAS: ras.permRAS,
    };
}

/**
 * The panoramic's view of the volume the grid loaded.
 *
 * @param {object} options
 * @param {object} options.cornerstone injected: `{cache, awaitVolumeLoad, readScalarData,
 *   fetchHeader}`.
 * @param {string} options.volumeId the id both surfaces derive from the page payload.
 * @param {string} options.url the loader URL, for the header range request.
 * @returns {{descriptor: Function, release: Function, volume: Function}}
 */
export function createVolumeSupply({ cornerstone, volumeId, url }) {
    const { cache, awaitVolumeLoad, readScalarData, fetchHeader } = cornerstone;
    let cached = null;
    let header = null;

    return {
        /** The Cornerstone volume, or null while the grid is still loading it. */
        volume() {
            return cache.getVolume?.(volumeId) ?? null;
        },

        /**
         * The RAS descriptor, materialised on first use and kept.
         *
         * @returns {Promise<object|null>} null while the volume is still arriving, which
         *   is what the surface waits on.
         */
        async descriptor() {
            if (cached) {
                return cached;
            }
            const volume = cache.getVolume?.(volumeId);
            if (!volume) {
                return null;
            }
            // The same three rules Phase 3 wrote for F19: `load()` returns undefined, an
            // unread voxel manager hands back an empty array rather than throwing, and a
            // permanently-failed frame still increments the processed count.
            await awaitVolumeLoad(volume);
            header = header ?? (await fetchHeader(url));
            cached = rasDescriptor({ scalarData: readScalarData(volume), header });
            return cached;
        },

        /** Drop the reoriented copy. A CBCT of it is not something to hold after teardown. */
        release() {
            cached = null;
        },
    };
}

/**
 * Fetch the segmentation the arch is fitted against.
 *
 * A near-verbatim port of what `panoramicSource.panorexSegmentationSource` did, which was
 * itself a port of `viewer_grid.js:332-377` -- a fetch and a memo, and neither ever
 * touched NiiVue. Carried across rather than rewritten so that the one thing that changes
 * in this migration is the thing that had to.
 *
 * @param {object} options
 * @param {object} options.source the page's `panorexSource`.
 * @param {(options: object) => string} options.volumeUrl the id builder.
 * @param {object} options.cache a mutable object used as the memo.
 * @returns {Promise<ArrayBuffer>}
 */
export async function fetchSegmentation({
    source, volumeUrl, cache, namespace = 'api', origin,
    fetchImpl = globalThis.fetch,
}) {
    if (!source?.segmentationFileId) {
        throw new Error('No paired panoramic segmentation source is available');
    }
    const fileId = String(source.segmentationFileId);
    const fileKey = source.segmentationFileKey || 'volume_nifti';
    const key = `${fileId}:${fileKey}`;

    if (!cache[key]) {
        const response = await fetchImpl(
            volumeUrl({
                fileId: Number(fileId),
                bundleKey: fileKey,
                filename: 'segmentation.nii.gz',
                namespace,
                origin,
            }),
            { credentials: 'same-origin' }
        );
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
        cache[key] = await response.arrayBuffer();
    }
    return cache[key];
}
