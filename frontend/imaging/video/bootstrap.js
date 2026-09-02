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

export const LOG_PREFIX = '[ygg-video]';

/**
 * The element carrying the surface's JSON payload.
 *
 * (`toolDecision` used to live here: a pure, exported, tested copy of the
 * "which tool may be armed" rule with no caller anywhere. `editor.setActiveTool` is the
 * one that runs, and it now answers `'ok' | 'unknown' | 'needs-region'` so the binder can
 * tell a missing region from a button naming a tool that does not exist. Two
 * implementations of one rule, in two shapes, is how they drift.)
 */
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
 * Read the stored state, or say why annotation is unavailable.
 *
 * Never throws and never returns a rejection: an endpoint that is down is a reason, not
 * an exception, because the caller's answer to it is to play the video anyway.
 *
 * @returns {Promise<{state: object|null, unavailable: string}>}
 */
export async function readState(endpoint, doFetch) {
    try {
        const response = await doFetch(endpoint, { credentials: 'same-origin' });
        if (!response.ok) {
            return { state: null, unavailable: `the annotation service answered HTTP ${response.status}` };
        }
        return { state: await response.json(), unavailable: '' };
    } catch (error) {
        return { state: null, unavailable: `the annotation service could not be reached: ${error.message}` };
    }
}

/**
 * Fetch the state, mount the editor, and hand the page a surface to wire to.
 *
 * **A failure to annotate must not cost the recording.** This used to return `null` the
 * moment the state endpoint answered anything but 200, and the page's glue then left the
 * viewport hidden and the placeholder -- "No video uploaded for this patient." -- on
 * screen. A server bug in an annotation endpoint therefore presented as a missing file,
 * over a video sitting in object storage exactly where it should be, and sent people
 * looking for an upload that had happened.
 *
 * So the surface mounts either way and reports which of the two it is:
 *
 *   - **Full.** The state was read; masks are loaded, the region types are known, drawing
 *     and saving work.
 *   - **Degraded.** The video plays, the frame navigation works, and nothing can be drawn
 *     or saved. `reason` says why, in a sentence a clinician can act on.
 *
 * The frame-size disagreement keeps its meaning and joins the second case rather than
 * blocking the first: a stored mask must never be painted over a differently-sized
 * recording, and the recording is still watchable while that is sorted out.
 *
 * @param {object} deps
 * @param {Function} deps.createEditor from `editor.js`, with Cornerstone bound in.
 * @param {Function} [deps.fetchImpl]
 * @param {Document} [deps.doc]
 * @returns {Promise<object|null>} the mounted surface, or `null` if there is no video.
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

    const { state, unavailable } = await readState(data.endpoint, doFetch);
    const size = frameSizeFor(state, data);
    const reason = unavailable || size.refusal || '';
    const canAnnotate = !reason;
    if (reason) {
        report(`${reason}; mounting for playback only`);
    }

    // The recording's own size when the record cannot be trusted or read. It is what the
    // viewport plays either way; only the masks care which of the two it is.
    const width = size.refusal ? data.width : size.width;
    const height = size.refusal ? data.height : size.height;

    const store = canAnnotate ? createMaskStore({ width, height }) : null;
    store?.load(state.frames);

    // Empty when nothing can be drawn, which is what makes every drawing tool refuse:
    // `TOOL_PLAN` marks brush, eraser and the scissors `needsRegion`, and there is no
    // region to select. The editor then registers no labelmaps either.
    // A mutable list: `editor.addRegion` extends it when an administrator creates a
    // region type without leaving the page.
    //
    // `regionTypes` is one array, built here and shared by `colorFor`, the exposed
    // property and `addRegionType`. It has to be the *same* array: a region created
    // mid-session is pushed onto whatever `addRegionType` holds, and `colorFor` is what
    // turns a code into the swatch its mask is painted in. Rebuilding it per reader --
    // which `state.regionTypes ?? []` does whenever the payload omits the key -- left the
    // lookup pointing at a list the new region was never added to, so the new region drew
    // in Cornerstone's default colour while the list beside it showed its own.
    const regionTypes = canAnnotate ? [...(state.regionTypes ?? [])] : [];
    const regionCodes = regionTypes.map((type) => type.name);
    const editor = await createEditor({
        element,
        instanceId: `patient-${data.patientId}`,
        videoImageId: videoImageId({ url: data.videoUrl, frameNumber: 1 }),
        // **The film to watch, which is not the film to annotate.** `videoUrl` is the
        // subsampled track -- one frame per source second, so playing it steps through
        // stills -- and `playbackUrl` is the compressed film of the same surgery at its
        // real frame rate. Both are addressed by the same clock (see
        // `_video_annotate_payload`), so watching one and filing masks against the other
        // costs nothing and changes no stored frame. Absent when there is only one film.
        playbackUrl: data.playbackUrl || null,
        fps: data.fps,
        store,
        regionCodes,
        frameIdFor: (frameNumber) => videoImageId({ url: data.videoUrl, frameNumber }),
        // The colour the region list shows beside a name is the colour its mask has to
        // be drawn in; without this every region paints in Cornerstone's default and the
        // swatches describe nothing. Read through a lookup rather than copied, so a
        // region added while the page is open is coloured too.
        colorFor: (regionCode) =>
            regionTypes.find((type) => type.name === regionCode)?.color,
    });

    let revision = state?.revision ?? 0;
    let currentTimeMs = 0;

    async function save() {
        if (!canAnnotate) {
            // Refused here rather than left to fail at the endpoint: a save built from a
            // store that never loaded would post an empty set over somebody's work, and
            // the endpoint's own answer to that is a 200.
            return { ok: false, conflict: false, degraded: true, message: reason };
        }
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
            return { ok: false, conflict: true, message: 'Another save landed first. Reload to rebase.' };
        }
        if (!result.ok) {
            report(`save failed with HTTP ${result.status}`);
            return { ok: false, conflict: false, message: `The save failed (HTTP ${result.status}).` };
        }
        revision = (await result.json()).revision ?? revision;
        dirty = false;
        return { ok: true, conflict: false, revision };
    }

    let dirty = false;

    const frameCount = Number.isFinite(data.frameCount) && data.frameCount > 0 ? data.frameCount : null;
    // The last frame's instant, which is where "go to end" lands and what every step
    // clamps against. Null when the probe did not state a frame count, in which case
    // nothing is clamped rather than everything being clamped to zero.
    const durationMs = frameCount ? msForFrameNumber(frameCount, data.fps) : null;

    report(canAnnotate ? 'mounted' : 'mounted for playback only', {
        patientId: data.patientId,
        width,
        height,
        regionCodes,
        reason,
    });
    await editor.showFrame(1, 0, null);
    return {
        editor,
        store,
        save,

        /** The patient this surface is showing, for the endpoints the page calls. */
        patientId: data.patientId,

        /** Whether anything can be drawn or saved on this mount, and why not. */
        canAnnotate,
        reason,

        /** Re-measure the canvas against its container -- see `editor.resize`. */
        resize: () => editor.resize(),

        /** The region types this study offers, in the order the server ranked them. */
        regionTypes,

        /**
         * Take on a region type created while this page was open.
         *
         * Returns false when annotation is unavailable at all, so the caller reports the
         * mount's own reason rather than a second one.
         */
        addRegionType(type) {
            if (!canAnnotate || !type?.name) {
                return false;
            }
            if (!regionTypes.some((known) => known.name === type.name)) {
                regionTypes.push(type);
            }
            return editor.addRegion(type.name);
        },

        /**
         * Forget a region type that was deleted while this page was open.
         *
         * The masks go with it: a region type that no longer exists cannot be listed,
         * drawn or exported, and leaving its planes in the store would put them back on
         * the next save under a code the project has no name for.
         */
        removeRegionType(regionCode) {
            const at = regionTypes.findIndex((known) => known.name === regionCode);
            if (at >= 0) {
                regionTypes.splice(at, 1);
            }
            // The store is this module's, and so is the cleanup -- `updateRegionType`
            // renames in it for the same reason. Leaving the masks would put them back on
            // the next save under a code the project no longer has a name for.
            store?.forget?.(regionCode);
            return editor.removeRegion(regionCode);
        },

        /**
         * Take on a rename or a recolour the server has already accepted.
         *
         * A rename moves every mask this session holds onto the new code: the archive is
         * keyed by region *code*, so masks left under the old name would come back as
         * orphans (`region_label_schema`'s `extra_codes` exists for exactly that, and this
         * is the client's side of not needing it).
         */
        updateRegionType(regionCode, { name, color } = {}) {
            const known = regionTypes.find((type) => type.name === regionCode);
            if (!known) {
                return false;
            }
            if (color) {
                known.color = color;
            }
            if (name && name !== regionCode) {
                known.name = name;
                editor.addRegion(name);
                store.rename?.(regionCode, name);
                editor.removeRegion(regionCode);
            }
            return true;
        },

        /**
         * Every mask this session holds, newest listing order, for the annotation list.
         *
         * Built here rather than in the binder because it joins two things the binder
         * should not have to know how to join: the store's `(instant, region)` index and
         * the region types' display names and colours.
         */
        annotations() {
            // **Flushed first.** The store is only written when a frame is left or a save
            // runs, so a stroke on the frame now on screen is still in Cornerstone's
            // buffer -- and a list built without this would not show the annotation the
            // reader just drew until they navigated away from it. `flush` compares before
            // it copies, so calling it on every redraw costs nothing when nothing moved.
            editor.flush(currentTimeMs);
            const rows = [];
            for (const timeMs of store.annotatedTimes()) {
                for (const regionCode of store.regionsAt(timeMs)) {
                    rows.push({
                        timeMs,
                        regionCode,
                        color: regionTypes.find((type) => type.name === regionCode)?.color ?? null,
                        tool: store.toolAt(timeMs, regionCode),
                    });
                }
            }
            return rows;
        },

        /** What the frame navigation needs, from the server's own ffprobe. */
        fps: data.fps,
        frameCount,
        durationMs,

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
            const snapped = snapToFrame(clampMs(timeMs, durationMs), data.fps);
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

/**
 * Keep an instant inside the recording.
 *
 * Exported because "step back ten seconds from the second frame" and "step forward from
 * the last" are the two cases the frame bar hits constantly, and both are arithmetic a
 * unit test can settle.
 *
 * @param {number} timeMs
 * @param {number|null} durationMs the end of the recording, if it is known.
 * @returns {number}
 */
export function clampMs(timeMs, durationMs) {
    const value = Number.isFinite(timeMs) ? timeMs : 0;
    if (value < 0) {
        return 0;
    }
    if (Number.isFinite(durationMs) && durationMs > 0 && value > durationMs) {
        return durationMs;
    }
    return value;
}

/** Django's CSRF cookie, which every write on this site carries. */
/**
 * The frame size the masks describe, or why the surface must not mount.
 *
 * **The record wins where there is one.** Taking the size from the video
 * unconditionally would silently re-base every stored mask the first time a study was
 * re-encoded, which is the one thing the export refuses to do and this must not do
 * quietly either.
 *
 * **And when the two disagree, neither wins.** Drawing a 1920x1080 mask over a 1280x720
 * recording puts every stored region somewhere it was not, at full opacity, with nothing
 * on screen looking wrong. A patient can reach this: the raw upload and the runner's
 * compressed derivative are separate registry rows carrying separate probes, and the
 * page plays whichever ranks highest that it can describe.
 * `manage.py laparoscopy_probe_videos` reports the same disagreement server-side; this
 * is the half that stops it being drawn.
 *
 * @param {object} state the stored annotation state.
 * @param {object} data the page payload.
 * @returns {{width: number, height: number}|{refusal: string}}
 */
export function frameSizeFor(state, data) {
    const width = state?.width || data?.width;
    const height = state?.height || data?.height;
    if (!width || !height) {
        return { refusal: 'neither the record nor the page states a frame size; declining to mount' };
    }
    if (
        state?.width &&
        data?.width &&
        (state.width !== data.width || state.height !== data.height)
    ) {
        return {
            refusal:
                `the stored annotations are ${state.width}x${state.height} and this video ` +
                `is ${data.width}x${data.height}; declining to mount rather than draw them ` +
                'over the wrong pixels',
        };
    }
    return { width, height };
}

/**
 * The CSRF token every write on this site carries.
 *
 * **The hidden input first.** `CSRF_USE_SESSIONS = True` (yggdrasil/settings.py:249)
 * keeps the token in the session, so there is no `csrftoken` cookie to read on this
 * deployment and a cookie-only reader returns the empty string -- which makes every save
 * a bare 403. The grid hit exactly this and its template carries a `{% csrf_token %}`
 * for the same reason; so does the laparoscopy page now. The cookie stays as a fallback
 * because it is what a Django deployment without that setting would offer.
 *
 * @param {Document} doc
 * @returns {string}
 */
export function readCsrfToken(doc) {
    const input = doc?.querySelector?.('input[name="csrfmiddlewaretoken"]')?.value;
    if (input) {
        return input;
    }
    const match = (doc?.cookie || '').match(/(?:^|;\s*)csrftoken=([^;]*)/);
    return match ? decodeURIComponent(match[1]) : '';
}
