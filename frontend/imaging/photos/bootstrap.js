/**
 * Mounting the photo stack on a patient-detail page.
 *
 * Same contract as `grid/bootstrap.js`: read the DOM, never throw into the page, and say
 * out loud when it declines to run. That last part is a lesson rather than a style --
 * the grid's first version returned `null` from three places with no output, and a blank
 * viewer that reports nothing is indistinguishable from a viewer that failed.
 *
 * Cornerstone reaches this module only through `mount`, which the entry supplies. Every
 * decision worth testing lives in the pure modules beside this one.
 */

import {
    PHOTO_CONTROL_IDS,
    bindControls,
    controlPlan,
    isAnnotationModeOn,
} from './controls.js';
import { webImageId } from '../loaders/webImageLoader.js';
import {
    buildStackSaveRequest,
    restorablesByImageId,
} from './photoMeasurements.js';
import { PHOTO_MEASUREMENT_TOOLS } from './stackViewport.js';
import { interpretSaveResponse } from '../annotations/protocol.js';
import {
    calibrationRequest,
    formatCalibration,
    recalibrationWarning,
} from './calibration.js';

export const LOG_PREFIX = '[ygg-photo]';

/** The element carrying the surface's JSON payload. */
export const DATA_ELEMENT_ID = 'photoStackData';

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
 * @returns {object|null} `{patientId, projectNamespace, modalitySlug, endpoint}`
 */
export function readPhotoData(doc) {
    const element = doc?.getElementById?.(DATA_ELEMENT_ID);
    if (!element) {
        return null;
    }
    try {
        const parsed = JSON.parse(element.textContent ?? '{}');
        if (!parsed?.patientId || !parsed?.endpoint) {
            return null;
        }
        return parsed;
    } catch (error) {
        report(`#${DATA_ELEMENT_ID} is not valid JSON: ${error.message}`);
        return null;
    }
}

/**
 * Django's CSRF token, the way this project issues it.
 *
 * The hidden input, not the cookie: `CSRF_USE_SESSIONS = True` means there is no
 * `csrftoken` cookie at all, and `CSRF_COOKIE_HTTPONLY` would block reading one if there
 * were. The grid's first version read the cookie, found nothing, and every save was a
 * bare 403 with Django's HTML error page instead of a message from the endpoint.
 */
export function csrfToken(doc) {
    return doc?.querySelector?.('input[name="csrfmiddlewaretoken"]')?.value ?? '';
}

/** `/{namespace}/api` , or `/api` for the global namespace. */
function apiPrefix(namespace) {
    return namespace === 'api' ? '/api' : `/${namespace}/api`;
}

/**
 * Turn the images endpoint's payload into the records everything else consumes.
 *
 * One shape from two endpoints: the teleradiography endpoint returns a single image and
 * the intraoral one a list, and normalising here keeps that difference out of every
 * consumer.
 *
 * @param {object} payload
 * @param {object} options `{namespace, origin}`
 * @returns {object[]}
 */
export function readImageRecords(payload, { namespace, origin } = {}) {
    const entries = Array.isArray(payload?.images)
        ? payload.images
        : payload?.url || payload?.file_id
          ? [payload]
          : [];

    return entries
        .map((entry, index) => {
            const fileId = Number(entry.file_id ?? entry.id);
            if (!Number.isInteger(fileId) || fileId <= 0) {
                return null;
            }
            // The filename segment is decorative -- the route resolves the file from the
            // id -- but `assertServableFilename` refuses one carrying `/`, `\`, `?` or
            // `#`, so an uploaded name is scrubbed rather than trusted. Dot runs are
            // collapsed too: `..` is harmless inside a single segment, but leaving it
            // there makes every future reader stop and check that it is.
            const filename =
                entry.original_filename?.replace(/[^\w.-]/g, '_').replace(/\.{2,}/g, '.') ||
                `image-${fileId}.jpg`;
            return {
                fileId,
                imageId: webImageId({ fileId, filename, namespace, origin }),
                index: entry.index ?? index + 1,
                originalFilename: entry.original_filename ?? '',
                width: entry.image_width ?? null,
                height: entry.image_height ?? null,
                pixelSpacingMm: entry.pixel_spacing_mm ?? null,
                calibration: entry.pixel_spacing_mm ?? null,
                isProcessed: Boolean(entry.is_processed),
                editMeta: entry.edit_meta ?? null,
                url: entry.url ?? null,
            };
        })
        .filter(Boolean);
}

/**
 * Mount the surface.
 *
 * @param {object} options
 * @param {Function} options.mount the Cornerstone-touching factory, from the entry.
 * @param {Document} [options.doc]
 * @returns {Promise<object|null>}
 */
export async function bootstrapPhotoStack({ mount, doc = globalThis.document }) {
    const data = readPhotoData(doc);
    if (!data) {
        report(`no #${DATA_ELEMENT_ID} on this page; nothing to mount.`);
        return null;
    }
    const plan = controlPlan(doc);
    if (!plan.viewport) {
        report(`no #${PHOTO_CONTROL_IDS.viewport} on this page; nothing to mount.`);
        return null;
    }

    const namespace = data.projectNamespace || 'maxillo';
    const origin = globalThis.location?.origin;
    const prefix = apiPrefix(namespace);

    let records = [];
    try {
        const response = await fetch(new URL(data.endpoint, origin).href, {
            credentials: 'same-origin',
        });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        records = readImageRecords(await response.json(), { namespace, origin });
    } catch (error) {
        report(`could not list the images: ${error.message}`);
        showMessage(plan.viewport, 'These images could not be listed.');
        return null;
    }

    if (!records.length) {
        report('the endpoint returned no usable images.');
        showMessage(plan.viewport, 'There are no images on this study yet.');
        return null;
    }

    const registry = new Map(records.map((record) => [record.imageId, record]));
    const stack = await mount({ element: plan.viewport, registry });
    if (!stack) {
        return null;
    }

    await stack.setStack(
        records.map((record) => record.imageId),
        0
    );

    const measurementsUrl = (suffix, params) => {
        const url = new URL(
            `${prefix}/patients/${data.patientId}/measurements${suffix}`,
            origin
        );
        for (const [key, value] of Object.entries(params ?? {})) {
            url.searchParams.set(key, String(value));
        }
        return url.href;
    };

    let revision = 0;
    try {
        const state = await fetch(
            measurementsUrl('/state/', {
                fileIds: records.map((record) => record.fileId).join(','),
            }),
            { credentials: 'same-origin' }
        );
        if (state.ok) {
            const body = await state.json();
            revision = Number(body.revision) || 0;
            const restorable = restorablesByImageId(
                body.images,
                new Map(records.map((record) => [record.fileId, record.imageId]))
            );
            const restored = stack.restoreAnnotations(restorable);
            report(`restored ${restored} measurement(s) at revision ${revision}.`);
        }
    } catch (error) {
        // A study whose measurements cannot be fetched is still worth showing.
        report(`could not read stored measurements: ${error.message}`);
    }

    const current = () => records[stack.currentIndex()] ?? records[0];

    const controls = bindControls({
        plan,
        annotationsOn: isAnnotationModeOn(plan),
        onTool: (toolName) => stack.setPrimaryTool(toolName),
        onPrev: async () => {
            await stack.scrollTo(Math.max(0, stack.currentIndex() - 1));
            afterScroll();
        },
        onNext: async () => {
            await stack.scrollTo(Math.min(records.length - 1, stack.currentIndex() + 1));
            afterScroll();
        },
        onAnnotationMode: (enabled) => {
            stack.setAnnotationMode(enabled);
            stack.setAnnotationsVisible(enabled, PHOTO_MEASUREMENT_TOOLS);
        },
        onSave: async () => {
            try {
                const body = buildStackSaveRequest({
                    images: records,
                    annotations: stack.readAnnotations(),
                    expectedRevision: revision,
                });
                const response = await fetch(measurementsUrl('/'), {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': csrfToken(doc),
                    },
                    body: JSON.stringify(body),
                });
                const parsed = await response.json().catch(() => ({}));
                const outcome = interpretSaveResponse(response, parsed);
                if (outcome.saved) {
                    revision = outcome.revision;
                    return { level: 'success', message: 'Measurements saved.' };
                }
                return {
                    level: outcome.reload ? 'warning' : 'danger',
                    message: outcome.message,
                };
            } catch (error) {
                return { level: 'danger', message: error.message };
            }
        },
        onClear: async () => {
            const removed = stack.clearAnnotations(PHOTO_MEASUREMENT_TOOLS);
            if (!removed) {
                return {
                    level: 'info',
                    message: 'There are no measurements on this study to remove.',
                };
            }
            return {
                level: 'success',
                message: 'Measurements removed. Save to make it permanent.',
            };
        },
        onCalibrate: async () => {
            const record = current();
            const line = pendingCalibrationLine(stack);
            if (!line) {
                controls.report(
                    'info',
                    'Draw a Length measurement over something whose real size you know, ' +
                        'then press Calibrate.'
                );
                return;
            }
            const knownLengthMm = Number(
                globalThis.prompt?.('How long is that line, in millimetres?')
            );
            let body;
            try {
                body = calibrationRequest({ ...line, knownLengthMm });
            } catch (error) {
                controls.report('danger', error.message);
                return;
            }
            try {
                const response = await fetch(
                    new URL(
                        `${prefix}/patient/${data.patientId}/images/${record.fileId}/calibration/`,
                        origin
                    ).href,
                    {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': csrfToken(doc),
                        },
                        body: JSON.stringify(body),
                    }
                );
                const parsed = await response.json().catch(() => ({}));
                if (!response.ok) {
                    controls.report('danger', parsed.error ?? `HTTP ${response.status}`);
                    return;
                }
                record.pixelSpacingMm = parsed.pixelSpacingMm;
                record.calibration = parsed.pixelSpacingMm;
                registry.set(record.imageId, record);
                controls.setCalibration(formatCalibration(parsed.pixelSpacingMm));
                const warning = recalibrationWarning(parsed.affectedMeasurements);
                controls.report(
                    warning ? 'warning' : 'success',
                    warning || 'Calibrated. Lengths on this image are now in millimetres.'
                );
                // The metadata provider reads the registry, but Cornerstone has already
                // cached the module for this image, so the stack is reset to pick it up.
                await stack.setStack(
                    records.map((entry) => entry.imageId),
                    stack.currentIndex()
                );
            } catch (error) {
                controls.report('danger', error.message);
            }
        },
        onEdit: () => {
            const record = current();
            const editor = globalThis.RGBImageEditor;
            if (!editor?.attachToImage) {
                controls.report('danger', 'The image editor is not available on this page.');
                return;
            }
            report('handing off to RGBImageEditor', { fileId: record.fileId });
            editor.openForFile?.({
                patientId: data.patientId,
                modalitySlug: data.modalitySlug,
                sourceFileId: record.fileId,
                url: record.url,
                // The editor writes a NEW FileRegistry row, so the stack has to be
                // rebuilt around the new id rather than refreshed in place.
                onSaved: () => globalThis.location?.reload?.(),
            });
        },
    });

    function afterScroll() {
        const record = current();
        controls.setCounter(stack.currentIndex(), records.length);
        controls.setCalibration(formatCalibration(record.calibration));
    }
    afterScroll();

    observeSize(plan.viewport, () => stack.resize());
    report(`mounted ${records.length} image(s).`);
    return stack;
}

/**
 * The two endpoints of the most recent Length annotation, in image pixels.
 *
 * Calibration reuses the Length tool rather than adding a bespoke one: the user has
 * already learned to draw a line, and a second line-drawing interaction that looked the
 * same but behaved differently would be worse than reusing the first.
 */
export function pendingCalibrationLine(stack) {
    const lengths = stack
        .readAnnotations()
        .filter((entry) => entry?.metadata?.toolName === 'Length')
        .filter((entry) => (entry?.data?.handles?.points ?? []).length === 2);
    const last = lengths[lengths.length - 1];
    if (!last) {
        return null;
    }
    const [pointA, pointB] = last.data.handles.points;
    return { pointA: [pointA[0], pointA[1]], pointB: [pointB[0], pointB[1]] };
}

function showMessage(element, message) {
    if (!element) return;
    const note = element.ownerDocument?.createElement?.('p');
    if (!note) return;
    note.className = 'text-muted small p-3 mb-0';
    note.textContent = message;
    element.replaceChildren?.(note);
}

/** Resize on container change, including the moment a hidden tab is shown. */
function observeSize(element, callback) {
    const Observer = globalThis.ResizeObserver;
    if (!Observer) {
        globalThis.addEventListener?.('resize', () => callback());
        return;
    }
    const observer = new Observer(() => callback());
    observer.observe(element);
}
