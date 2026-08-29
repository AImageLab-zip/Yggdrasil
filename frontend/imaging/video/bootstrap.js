/**
 * Mounting the laparoscopy video annotator on a patient-detail page.
 *
 * Same contract as `grid/bootstrap.js` and `photos/bootstrap.js`: read the DOM, never
 * throw into the page, and say out loud when it declines to run. That last part is a
 * lesson rather than a style -- the grid's first version returned `null` from three
 * places with no output, and a blank viewer that reports nothing is indistinguishable
 * from one that failed.
 *
 * Cornerstone reaches this module only through `mount`, which the entry supplies. Every
 * decision worth testing lives in the pure modules beside it.
 */

import { buildSaveRequest, createMaskStore } from './masks.js';
import {
    currentInstantMs,
    frameNumberForMs,
    msForFrameNumber,
    snapToFrame,
} from './frameIdentity.js';
import { videoImageId } from './metadata.js';
import { TOOL_PLAN } from './editor.js';

export const LOG_PREFIX = '[ygg-video]';

/** The element carrying the surface's JSON payload. */
export const DATA_ELEMENT_ID = 'videoAnnotateData';

/** Say what the bootstrap decided, and why. */
export function report(message, detail) {
    const line = `${LOG_PREFIX} ${message}`;
    if (detail === undefined) {
        console.info(line);
    } else {
        console.info(line, detail);
    }
}

/**
 * Read the surface's payload.
 *
 * @param {Document} doc
 * @returns {object|null} `{patientId, videoUrl, fps, endpoint, canModify}`
 */
export function readVideoData(doc, elementId = DATA_ELEMENT_ID) {
    const node = doc.getElementById(elementId);
    if (!node) {
        return null;
    }
    try {
        const payload = JSON.parse(node.textContent || 'null');
        if (!payload || typeof payload !== 'object') {
            return null;
        }
        if (!payload.videoUrl || !Number.isInteger(payload.patientId)) {
            return null;
        }
        return payload;
    } catch (error) {
        report('payload is not valid JSON; declining to mount', error);
        return null;
    }
}

/**
 * What the toolbar should do with the current selection.
 *
 * Pure, so the "you must pick a region first" rule is a unit test rather than a click.
 *
 * @param {string} key a toolbar button's data-tool value.
 * @param {string|null} activeRegion
 * @returns {{allowed: boolean, tool: string|null, reason: string}}
 */
export function toolDecision(key, activeRegion) {
    const plan = TOOL_PLAN[key];
    if (!plan) {
        return { allowed: false, tool: null, reason: `no such tool: ${key}` };
    }
    if (plan.needsRegion && !activeRegion) {
        return {
            allowed: false,
            tool: plan.tool,
            reason: 'pick a region before drawing on one',
        };
    }
    return { allowed: true, tool: plan.tool, reason: '' };
}

/**
 * Fetch the state, mount the editor, and wire the page's controls to it.
 *
 * @param {object} deps
 * @param {Function} deps.createEditor from `editor.js`, with Cornerstone bound in.
 * @param {Function} [deps.fetchImpl]
 * @param {Document} [deps.doc]
 * @returns {Promise<object|null>} the mounted surface, or `null` if it declined.
 */
export async function mountVideoAnnotator({ createEditor, fetchImpl, doc } = {}) {
    const document_ = doc ?? globalThis.document;
    const doFetch = fetchImpl ?? ((...args) => globalThis.fetch(...args));

    const data = readVideoData(document_);
    if (!data) {
        report('no payload on this page; not a video study');
        return null;
    }
    const element = document_.getElementById('video-annotate-viewport');
    if (!element) {
        report('the page has no #video-annotate-viewport element; declining to mount');
        return null;
    }

    const response = await doFetch(data.endpoint, { credentials: 'same-origin' });
    if (!response.ok) {
        report(`state endpoint answered HTTP ${response.status}; declining to mount`);
        return null;
    }
    const state = await response.json();

    // The frame size comes from the *record* when there is one, and from the video
    // otherwise. Taking it from the video unconditionally would silently re-base every
    // stored mask the first time a study was re-encoded, which is the one thing the
    // export refuses to do and this must not do quietly either.
    const width = state.width || data.width;
    const height = state.height || data.height;
    if (!width || !height) {
        report('neither the record nor the page states a frame size; declining to mount');
        return null;
    }

    const store = createMaskStore({ width, height });
    store.load(state.frames);

    const regionCodes = (state.regionTypes ?? []).map((type) => type.name);
    const editor = await createEditor({
        element,
        instanceId: `patient-${data.patientId}`,
        videoImageId: videoImageId({ url: data.videoUrl, frameNumber: 1 }),
        fps: data.fps,
        store,
        regionCodes,
        frameIdFor: (frameNumber) => videoImageId({ url: data.videoUrl, frameNumber }),
    });

    let revision = state.revision ?? 0;
    let currentTimeMs = 0;

    async function save() {
        editor.flush(currentTimeMs);
        const body = buildSaveRequest({
            store,
            prompts: state.prompts ?? [],
            expectedRevision: revision,
        });
        const result = await doFetch(data.endpoint, {
            method: 'PUT',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': readCsrfToken(document_),
            },
            body: JSON.stringify(body),
        });
        if (result.status === 409) {
            // Somebody else saved while this editor was open. Re-reading is the only
            // honest recovery: overwriting would discard work this session never saw.
            report('another save landed first; reload to rebase');
            return { ok: false, conflict: true };
        }
        if (!result.ok) {
            report(`save failed with HTTP ${result.status}`);
            return { ok: false, conflict: false };
        }
        revision = (await result.json()).revision ?? revision;
        dirty = false;
        return { ok: true, conflict: false, revision };
    }

    let dirty = false;

    report('mounted', { patientId: data.patientId, width, height, regionCodes });
    await editor.showFrame(1, 0, null);
    return {
        editor,
        store,
        save,
        /** Is there work the user would lose by navigating away? */
        get dirty() {
            return dirty;
        },
        markDirty() {
            dirty = true;
        },
        get revision() {
            return revision;
        },
        /** The instant a click on the player should annotate. */
        instantFor(video, frameMetadata) {
            return snapToFrame(currentInstantMs(video, frameMetadata), data.fps);
        },

        /** Move to the frame containing an instant. */
        async goToInstant(timeMs) {
            const snapped = snapToFrame(timeMs, data.fps);
            const frameNumber = frameNumberForMs(snapped, data.fps);
            await editor.showFrame(
                frameNumber,
                msForFrameNumber(frameNumber, data.fps),
                currentTimeMs
            );
            currentTimeMs = msForFrameNumber(frameNumber, data.fps);
            return currentTimeMs;
        },

        get timeMs() {
            return currentTimeMs;
        },
    };
}

/** Django's CSRF cookie, which every write on this site carries. */
export function readCsrfToken(doc) {
    const match = (doc.cookie || '').match(/(?:^|;\s*)csrftoken=([^;]*)/);
    return match ? decodeURIComponent(match[1]) : '';
}
