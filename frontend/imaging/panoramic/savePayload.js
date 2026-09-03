/**
 * The panoramic's two wire contracts: what a save posts, and what it announces.
 *
 * Both are kept here, pure and away from the DOM, because both have consumers that fail
 * *silently* when they change.
 *
 * **The save body** is read by `maxillo/views/patient_data.py::save_browser_panoramic`,
 * whose `_normalize_browser_panoramic_state` refuses anything it does not recognise. Phase
 * 7 leaves that endpoint's validation exactly as it was, so this shape is fixed.
 *
 * **The announcement** is read by `static/js/panoramic_warmup.js:158-163`, which drives
 * patient pages in a hidden frame and advances on the message. A page that never sends one
 * does not fail -- it waits out a five-minute timeout, per patient, for a whole folder. So
 * every path out of the surface announces, including the ones that decline to run, and the
 * outcomes are enumerated here rather than spelled at each call site.
 */

/** The baker this surface speaks for. Stored, and checked on the way back in. */
export const ALGORITHM_VERSION = 'panorex-js-v2-mip';

/** What an unattended pass can report. `static/js/panoramic_warmup.js:64-74` maps each. */
export const OUTCOMES = Object.freeze({
    /** Nothing to generate: no volume, no segmentation, no permission. */
    SKIPPED: 'skipped',
    /** A current panoramic was already stored. */
    EXISTING: 'existing',
    /** One was generated and saved. */
    CREATED: 'created',
    /** It was attempted and did not work. */
    FAILED: 'failed',
});

/** The DOM event name, for anything on the page itself. */
export const ANNOUNCE_EVENT = 'panoramicdefault';

/** The `postMessage` discriminator, for the warm-up frame. */
export const ANNOUNCE_TYPE = 'panoramic-default';

/**
 * The `state` part of the multipart save.
 *
 * Snake_case throughout: the endpoint accepts both spellings through `_input_value`, and
 * matching the field names it validates against keeps the two readable side by side.
 *
 * @param {object} options
 * @param {object} options.source the page's `panorexSource` payload.
 * @param {object} options.dimensions `{width, height, depth}` of the RAS volume.
 * @param {number} options.axialSlice
 * @param {number[][]} options.controlPoints the arch, in slice pixels.
 * @param {string} options.geometrySource `'auto'` or `'custom_cp'`.
 * @param {string} options.mode `'mip'` or `'raysum'`.
 * @param {string} options.generationUuid
 * @returns {object}
 */
export function saveState({
    source, dimensions, axialSlice, controlPoints, geometrySource, mode, generationUuid,
}) {
    return {
        source: {
            job_id: source.jobId,
            file_id: source.volumeFileId,
            file_key: source.volumeFileKey,
            file_hash: source.volumeFileHash,
            segmentation_file_id: source.segmentationFileId,
            segmentation_file_key: source.segmentationFileKey,
            segmentation_file_hash: source.segmentationFileHash,
        },
        volume_shape: [dimensions.width, dimensions.height, dimensions.depth],
        axial_slice: axialSlice,
        // Copied rather than passed through: the control points are live editor state and
        // a save must not hand the request a reference somebody can still drag.
        spline: controlPoints.map((point) => [point[0], point[1]]),
        geometry_source: geometrySource,
        default_mode: mode,
        algorithm_version: ALGORITHM_VERSION,
        generation_uuid: generationUuid,
        base_revision: Number(source.revision) || 0,
    };
}

/**
 * The multipart body.
 *
 * @param {object} state from {@link saveState}.
 * @param {{mip: Blob, raysum: Blob}} strips
 * @param {typeof FormData} [FormDataImpl] injection seam for tests.
 */
export function saveBody(state, strips, FormDataImpl = globalThis.FormData) {
    const form = new FormDataImpl();
    form.append('state', JSON.stringify(state));
    form.append('mip_png', strips.mip, 'panoramic-mip.png');
    form.append('raysum_png', strips.raysum, 'panoramic-raysum.png');
    return form;
}

/**
 * What a save response means.
 *
 * The 409-while-silent case is the one worth naming: another tab, or an earlier visit,
 * already wrote the default for this exact source. Nothing is wrong and nothing is left to
 * do, so it reports `existing` rather than `failed` -- a warm-up run over a folder that
 * had already been warmed would otherwise come back a wall of red.
 *
 * @param {{status: number, ok: boolean}} response
 * @param {object|null} payload the parsed body, if there was one.
 * @param {boolean} silent whether this was an unattended pass.
 * @returns {{saved: boolean, outcome: string, revision: number|null, message: string,
 *   conflicted: boolean}}
 */
export function interpretSave(response, payload, silent) {
    const data = payload ?? {};
    if (response.status === 409 && silent) {
        return {
            saved: true, conflicted: true, outcome: OUTCOMES.EXISTING,
            revision: null, message: '',
        };
    }
    if (!response.ok) {
        return {
            saved: false, conflicted: false, outcome: OUTCOMES.FAILED, revision: null,
            message: data.error || data.detail || `Save failed (HTTP ${response.status}).`,
        };
    }
    return {
        saved: true, conflicted: false, outcome: OUTCOMES.CREATED,
        revision: data.revision ?? null, message: '',
    };
}

/**
 * The payload both announcement channels carry.
 *
 * @param {string} outcome one of {@link OUTCOMES}.
 * @param {string|number|null} patientId
 * @param {string} [detail] why, for the warm-up log.
 */
export function announcement(outcome, patientId, detail = null) {
    return { type: ANNOUNCE_TYPE, outcome, patientId: patientId ?? null, detail: detail || null };
}

/**
 * Whether a stored arch can be restored into the editor.
 *
 * Four conditions, and each has a reason. A superseded baker's arch is history, not
 * something to reopen. Fewer than four control points cannot be re-fitted -- the endpoint
 * refuses that many on the way in, and `catmullRomChain` needs four to draw a segment. And
 * a shape that disagrees with the volume now loaded means the arch was drawn on something
 * else, whatever the revision says.
 *
 * @param {object|null} state the page payload's `panorexSource.state`.
 * @param {{width: number, height: number, depth: number}} dimensions
 * @returns {boolean}
 */
export function canRestore(state, dimensions) {
    if (!state || state.algorithmVersion !== ALGORITHM_VERSION) {
        return false;
    }
    if (!Array.isArray(state.spline) || state.spline.length < 4) {
        return false;
    }
    const shape = state.volumeShape;
    return Array.isArray(shape)
        && shape[0] === dimensions.width
        && shape[1] === dimensions.height
        && shape[2] === dimensions.depth;
}

/**
 * Whether a panoramic from the current baker is already stored.
 *
 * The revision is the server's own count and is zero for a patient who has none -- or for
 * one whose CBCT was replaced, which the server reports the same way on purpose: the arch
 * it holds describes bytes nobody is looking at.
 */
export function hasSavedPanoramic(source) {
    return Boolean(
        source
        && Number(source.revision) > 0
        && source.state
        && source.state.algorithmVersion === ALGORITHM_VERSION
    );
}

/**
 * A v4 UUID for one generation, from the platform's own generator where it exists.
 *
 * The endpoint keys idempotency on this: the same UUID with the same bytes is a repeat and
 * is answered 200, and with *different* bytes is a 409. So it has to change whenever the
 * pixels do -- which is why re-baking and switching mode both mint a new one.
 */
export function generationUuid(crypto = globalThis.crypto) {
    if (typeof crypto?.randomUUID === 'function') {
        return crypto.randomUUID();
    }
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
