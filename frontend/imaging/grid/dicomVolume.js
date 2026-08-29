/**
 * Prepare one stored DICOM series for the same volume path a NIfTI takes.
 *
 * The grid loads a volume in three steps: get a header, get the imageIds, hand both to
 * `volumeLoader.createAndCacheVolume`. For NIfTI those come from
 * `fetchHeader` and `createNiftiImageIdsAndCacheMetadata`. This module is the DICOM
 * answer to exactly the same two questions, and deliberately nothing more: everything
 * after it -- the volume cache, `awaitVolumeLoad`, the VOI, the orientation overlay,
 * the measurement tools -- is untouched and cannot tell the two apart.
 *
 * One thing here has no NIfTI counterpart. The wadors loader **does not fetch
 * metadata**: `metaDataManager.add(imageId, document)` is called by the application, and
 * the loader looks the result up per frame while decoding. So registering the metadata
 * is a step, not a side effect, and an id whose metadata was never registered fails
 * inside the decoder with no mention of what is missing.
 *
 * `metaDataManager` is injected rather than imported so this module stays loadable
 * under `node --test`; the entry passes Cornerstone's real one.
 */

import { dicomImageIds, orderInstances, seriesMetadataPath, toAbsolute } from '../ids/dicomImageIds.js';
import { dicomSeriesHeader } from '../metadata/dicomSeriesHeader.js';

/**
 * The URL the series' metadata is fetched from.
 *
 * Doubles as the volume's cache key (`volumeIdFor`), which is why it is derived rather
 * than passed: it is one per series, stable across reloads, and unique -- the same
 * three properties the NIfTI serve URL has.
 *
 * @param {object} options
 * @param {string} options.studyUid
 * @param {string} options.seriesUid
 * @param {string} [options.origin]
 * @returns {string}
 */
export function dicomSeriesUrl({ studyUid, seriesUid, origin }) {
    return toAbsolute(seriesMetadataPath({ studyUid, seriesUid }), { origin });
}

/**
 * Fetch a series' metadata, register it, and return what the volume path needs.
 *
 * @param {object} options
 * @param {string} options.studyUid
 * @param {string} options.seriesUid
 * @param {object} options.metaDataManager Cornerstone's wadors `metaDataManager`.
 * @param {Function} [options.fetchImpl] defaults to `globalThis.fetch`.
 * @param {string} [options.origin]
 * @returns {Promise<{imageIds: string[], header: object, instances: object[]}>}
 */
export async function prepareDicomSeries({
    studyUid,
    seriesUid,
    metaDataManager,
    fetchImpl,
    origin,
}) {
    const request = fetchImpl ?? globalThis.fetch;
    const url = dicomSeriesUrl({ studyUid, seriesUid, origin });
    const response = await request(url, { credentials: 'same-origin' });
    if (!response?.ok) {
        throw new Error(
            `Could not read this series (HTTP ${response?.status ?? '?'}). ` +
                'The study may have been removed, or this account may not have access to it.'
        );
    }

    const instances = await response.json();
    if (!Array.isArray(instances) || instances.length === 0) {
        throw new Error('This series has no instances stored.');
    }

    const entries = dicomImageIds({ studyUid, seriesUid, instances, origin });
    for (const entry of entries) {
        // Without this the frame downloads fine and the decoder then has no Rows,
        // Columns or transfer syntax to decode it with.
        metaDataManager.add(entry.imageId, entry.instance);
    }

    return {
        imageIds: entries.map((entry) => entry.imageId),
        // The *unique* documents: dicomSeriesHeader counts frames from
        // NumberOfFrames, so feeding it one entry per frame would double-count a
        // multi-frame series and halve its slice spacing.
        header: dicomSeriesHeader(orderInstances(instances)),
        instances,
    };
}
