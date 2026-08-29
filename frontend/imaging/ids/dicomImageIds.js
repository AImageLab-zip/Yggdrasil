/**
 * Build the `wadors:` imageIds Cornerstone's DICOM loader accepts.
 *
 * The sibling of `imageIds.js`, and the same kind of module for the same reason: every
 * rule below was read out of `@cornerstonejs/dicom-image-loader@5.8.2` rather than
 * inferred from the DICOMweb standard, because where the two differ it is the shipped
 * package that decides whether a study renders.
 *
 *   1. **The scheme prefix is stripped by index, not by name.**
 *      `imageLoader/imageIdToURI.js` is `imageId.substring(imageId.indexOf(':') + 1)`,
 *      so everything after the first colon is the URL. An `https://` URL is fine (the
 *      colon it contains is not the first one), but the prefix must be exactly
 *      `wadors:` with nothing before it.
 *   2. **The URL must end `/frames/<n>`, and `<n>` is 1-based.**
 *      `wadors/metaDataManager.js` finds a multi-frame instance's metadata by slicing
 *      the URI at `indexOf('/frames/') + 8` and parsing an integer off the end, then
 *      looking up the sibling id ending `1`. A 0-based frame number silently misses.
 *   3. **Metadata is pushed in, not fetched.** The loader never calls a `/metadata`
 *      endpoint; the application fetches it and calls `metaDataManager.add(imageId,
 *      document)` per instance. That is why {@link dicomImageIds} takes the metadata
 *      documents as its input -- the id and the metadata are built from one source, so
 *      they cannot disagree about which instance is which.
 *
 * Pure: no Cornerstone import, no DOM, no fetch.
 */

/** The loader's own scheme prefix (`registerImageLoader('wadors', ...)`). */
export const WADORS_SCHEME = 'wadors';

/** DICOM JSON tags this module reads. Eight uppercase hex digits, per PS3.18 F.2. */
export const TAG = Object.freeze({
    SOP_INSTANCE_UID: '00080018',
    INSTANCE_NUMBER: '00200013',
    NUMBER_OF_FRAMES: '00280008',
});

/**
 * Read the first value of a DICOM JSON element.
 *
 * Returns `undefined` for an absent element *and* for one present with no `Value`,
 * which DICOM JSON uses for a zero-length attribute -- the de-identifier emits several.
 *
 * @param {object} document a DICOM JSON instance document.
 * @param {string} tag eight uppercase hex digits.
 * @returns {*}
 */
export function firstValue(document, tag) {
    const element = document?.[tag];
    if (!element || !Array.isArray(element.Value) || element.Value.length === 0) {
        return undefined;
    }
    return element.Value[0];
}

/**
 * Path of the series metadata endpoint (`common/dicom/views.py::series_metadata`).
 *
 * @param {object} options
 * @param {string} options.studyUid pseudonymous StudyInstanceUID.
 * @param {string} options.seriesUid pseudonymous SeriesInstanceUID.
 * @returns {string}
 */
export function seriesMetadataPath({ studyUid, seriesUid }) {
    assertUid(studyUid, 'studyUid');
    assertUid(seriesUid, 'seriesUid');
    return `/api/dicomweb/studies/${studyUid}/series/${seriesUid}/metadata`;
}

/**
 * Path of one frame (`common/dicom/views.py::instance_frames`).
 *
 * @param {object} options
 * @param {string} options.studyUid
 * @param {string} options.seriesUid
 * @param {string} options.sopUid
 * @param {number} [options.frame] 1-based; see rule (2).
 * @returns {string}
 */
export function framePath({ studyUid, seriesUid, sopUid, frame = 1 }) {
    assertUid(sopUid, 'sopUid');
    if (!Number.isInteger(frame) || frame < 1) {
        throw new Error(
            `Frame numbers are 1-based integers, got ${JSON.stringify(frame)}. ` +
                'The loader parses this segment with parseInt and looks up the ' +
                "instance whose id ends '1'; a 0 resolves to nothing."
        );
    }
    const base = seriesMetadataPath({ studyUid, seriesUid }).replace(/\/metadata$/, '');
    return `${base}/instances/${sopUid}/frames/${frame}`;
}

/**
 * Reject a UID that would not survive the round trip through the URL.
 *
 * Deliberately strict, and cheap to satisfy: every UID this codebase stores is derived
 * by `common.dicom.deidentify.pseudonymous_uid` and is digits and dots by
 * construction. Anything else is a bug upstream, not a UID needing escaping.
 *
 * @param {string} uid
 * @param {string} what name used in the error.
 */
export function assertUid(uid, what) {
    if (typeof uid !== 'string' || uid.length === 0) {
        throw new Error(`${what} is required.`);
    }
    if (!/^[0-9.]+$/.test(uid)) {
        throw new Error(
            `${what} must be a DICOM UID (digits and dots), got '${uid}'. ` +
                'Yggdrasil stores only derived UIDs, so anything else means the ' +
                'value did not come from the catalog.'
        );
    }
}

/**
 * Make a root-relative path absolute, so the loader's own `new URL(...)` cannot throw.
 *
 * @param {string} path
 * @param {object} [options]
 * @param {string} [options.origin] defaults to the document origin.
 * @returns {string}
 */
export function toAbsolute(path, { origin } = {}) {
    const base = origin ?? globalThis.location?.origin;
    if (!base) {
        throw new Error('No origin available: pass options.origin explicitly.');
    }
    return new URL(path, base).href;
}

/**
 * Instance documents in the order they should stack.
 *
 * By `InstanceNumber` then SOPInstanceUID -- the same key `common.dicom.ingest` writes
 * them in, so the browser and the catalog agree on slice order without either trusting
 * the other's sort. Exported separately from {@link dicomImageIds} because the series
 * header is built from the *unique* documents while the ids are per frame.
 *
 * @param {object[]} instances
 * @returns {object[]}
 */
export function orderInstances(instances) {
    if (!Array.isArray(instances) || instances.length === 0) {
        throw new Error('A series needs at least one instance document.');
    }
    return [...instances].sort((left, right) => {
        const byNumber =
            (Number(firstValue(left, TAG.INSTANCE_NUMBER)) || 0) -
            (Number(firstValue(right, TAG.INSTANCE_NUMBER)) || 0);
        if (byNumber !== 0) {
            return byNumber;
        }
        return String(firstValue(left, TAG.SOP_INSTANCE_UID) ?? '').localeCompare(
            String(firstValue(right, TAG.SOP_INSTANCE_UID) ?? '')
        );
    });
}

/**
 * The full imageId list for one series, one id per frame, in stacking order.
 *
 * A multi-frame instance contributes one id per frame, because a Cornerstone volume is
 * a flat list of frames and `NumberOfFrames > 1` is otherwise invisible to it.
 *
 * @param {object} options
 * @param {string} options.studyUid
 * @param {string} options.seriesUid
 * @param {object[]} options.instances DICOM JSON documents, as the metadata endpoint
 *   returns them.
 * @param {string} [options.origin]
 * @returns {{imageId: string, sopUid: string, frame: number, instance: object}[]}
 */
export function dicomImageIds({ studyUid, seriesUid, instances, origin }) {
    const ordered = orderInstances(instances);
    const ids = [];
    for (const document of ordered) {
        const sopUid = String(firstValue(document, TAG.SOP_INSTANCE_UID) ?? '');
        const frames = Math.max(1, Number(firstValue(document, TAG.NUMBER_OF_FRAMES)) || 1);
        for (let frame = 1; frame <= frames; frame += 1) {
            ids.push({
                sopUid,
                frame,
                // The document this id was built from, so a caller registering
                // metadata cannot pair an id with the wrong instance -- the ordering
                // is decided once, here.
                instance: document,
                imageId: `${WADORS_SCHEME}:${toAbsolute(
                    framePath({ studyUid, seriesUid, sopUid, frame }),
                    { origin }
                )}`,
            });
        }
    }
    return ids;
}
