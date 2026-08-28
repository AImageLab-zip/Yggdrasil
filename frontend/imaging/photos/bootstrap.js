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
import { askForNumber, openPanel } from './dialog.js';
import { checkRoundTrip } from './coordinates.js';
import {
    SEGMENTATION_CONTROL_IDS,
    createToothEditor,
    segmentationControlPlan,
} from './toothSegmentation.js';

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
export function readPhotoData(doc, elementId = DATA_ELEMENT_ID) {
    const element = doc?.getElementById?.(elementId);
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
        report(`#${elementId} is not valid JSON: ${error.message}`);
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
export async function bootstrapPhotoStack({
    mount,
    doc = globalThis.document,
    dataElementId = DATA_ELEMENT_ID,
    ids = PHOTO_CONTROL_IDS,
    instanceId = 'stack',
}) {
    const data = readPhotoData(doc, dataElementId);
    if (!data) {
        report(`no #${dataElementId} on this page; nothing to mount.`);
        return null;
    }
    const plan = controlPlan(doc, ids);
    if (!plan.viewport) {
        report(`no #${ids.viewport} on this page; nothing to mount.`);
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
    const mounted = await mount({ element: plan.viewport, registry, instanceId });
    if (!mounted) {
        return null;
    }
    const { stack, worldToImage, imageToWorld } = mounted;

    await stack.setStack(
        records.map((record) => record.imageId),
        0
    );

    // One assertion per mount, on the *plane module* rather than the arithmetic: the two
    // converters are inverses by construction, so a round trip that does not close means
    // the metadata provider handed them something they cannot both work from -- a missing
    // imagePositionPatient, or cosines that are not orthonormal. That produces a mapping
    // silently wrong in a way no single measurement would reveal.
    const trip = checkRoundTrip(records[0].imageId, { worldToImage, imageToWorld });
    if (!trip.ok) {
        report(`world/image round trip is off by ${trip.deviation}; refusing to measure.`);
        showMessage(
            plan.viewport,
            'This image cannot be measured: its geometry does not round-trip. ' +
                'Nothing has been saved.'
        );
        return null;
    }

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
                new Map(records.map((record) => [record.fileId, record.imageId])),
                imageToWorld
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
                    toImage: worldToImage,
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
                    return { type: 'success', message: 'Measurements saved.' };
                }
                // A failure is still reported. The toast is green on success and red on
                // failure, which is the fix; making a failed save *look* successful would
                // mean a clinician closing the tab believing work is stored.
                return {
                    type: outcome.reload ? 'warning' : 'danger',
                    message: outcome.message,
                };
            } catch (error) {
                return { type: 'danger', message: error.message };
            }
        },
        onClear: async () => {
            const removed = stack.clearAnnotations(PHOTO_MEASUREMENT_TOOLS);
            if (!removed) {
                return {
                    type: 'info',
                    message: 'There are no measurements on this study to remove.',
                };
            }
            return {
                type: 'success',
                message: 'Measurements removed. Save to make it permanent.',
            };
        },
        onCalibrate: async () => {
            const record = current();
            const line = pendingCalibrationLine(stack, worldToImage, record.imageId);
            if (!line) {
                // Guidance, not an error. The tool needed is already on the toolbar, so
                // saying which one and what to do with it is the whole of the fix.
                controls.report(
                    'info',
                    'First switch annotations on and draw a Length line across something ' +
                        'whose real size you know -- a ruler, a known implant, a scale bar. ' +
                        'Then press Calibrate and enter that length.'
                );
                return;
            }

            const knownLengthMm = await askForNumber({
                title: 'Calibrate this image',
                message:
                    `The line you drew is ${Math.round(line.pixelDistance)} px long. ` +
                    'How long is it in reality?',
                unit: 'mm',
                min: 0,
                placeholder: 'e.g. 10',
            });
            if (knownLengthMm === null) {
                return;
            }

            let body;
            try {
                body = calibrationRequest({
                    pointA: line.pointA,
                    pointB: line.pointB,
                    knownLengthMm,
                });
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
                    warning || 'Calibrated. Lengths on this image now read in millimetres.'
                );
                // The provider reads the registry, but Cornerstone has already cached the
                // plane module for this image, so the stack is reset to make it re-ask.
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
            // `attachToImage(img, options)` is the editor's ONLY public method. The first
            // version of this called a guessed `openForFile()` behind optional chaining,
            // so the button did nothing at all and said nothing about it.
            if (typeof editor?.attachToImage !== 'function') {
                controls.report(
                    'danger',
                    'The image editor is not loaded on this page, so this image cannot be ' +
                        'cropped or rotated here.'
                );
                return;
            }
            if (!record.url) {
                controls.report('danger', 'This image has no served URL to edit.');
                return;
            }

            let edited = false;
            const panel = openPanel({
                title: 'Crop, mirror or rotate',
                icon: 'fa-crop',
                doc,
                onClose: async () => {
                    if (!edited) {
                        return;
                    }
                    // The editor writes a NEW FileRegistry row, so the stack has to be
                    // rebuilt around the new id rather than refreshed in place -- the old
                    // imageId now names a superseded file.
                    controls.report('info', 'Reloading the image…');
                    globalThis.location?.reload?.();
                },
            });

            // The editor mounts its own toolbar into the image's container and needs the
            // image to have loaded before it can read naturalWidth, so it is attached on
            // load rather than immediately.
            const host = doc.createElement('div');
            host.style.position = 'relative';
            const img = doc.createElement('img');
            img.className = 'img-fluid';
            img.style.maxWidth = '100%';
            img.alt = record.originalFilename || 'Image being edited';
            host.appendChild(img);
            panel.body.appendChild(host);

            img.addEventListener(
                'load',
                () => {
                    editor.attachToImage(img, {
                        patientId: data.patientId,
                        modalitySlug: data.modalitySlug,
                        sourceFileId: record.fileId,
                        container: host,
                        onSaved: () => {
                            edited = true;
                        },
                    });
                },
                { once: true }
            );
            img.addEventListener('error', () => {
                controls.report('danger', 'That image could not be loaded for editing.');
                panel.close();
            });
            img.src = new URL(record.url, origin).href;
        },
    });

    // -- tooth segmentation, on the intraoral surface only ------------------
    //
    // Mounted on the same stack rather than as a second surface: intraoral photographs are
    // a photo stack that also carries segmentation, and they need the same scroll, pan,
    // zoom, calibration and measurement tools. `data.segmentation` is what the template
    // says to switch it on, so teleradiography -- which has no teeth -- gets none of it.
    const editor = data.segmentation
        ? await mountSegmentation({
              doc,
              mounted,
              stack,
              records,
              data,
              prefix,
              origin,
              report,
              notify: controls.report,
          })
        : null;

    function afterScroll() {
        const record = current();
        controls.setCounter(stack.currentIndex(), records.length);
        controls.setCalibration(formatCalibration(record.calibration));
        // The editor draws one image's outlines at a time, so scrolling the stack has to
        // tell it which image is now on screen. Without this, outlines from image 1 stay
        // drawn over image 2 -- in the wrong place, and attributed to the wrong file on
        // the next save.
        void editor?.showImage(record.fileId);
    }
    afterScroll();
    // Also on the wheel. `StackScroll` moves the stack without going through `scrollTo`, so
    // the counter, the calibration readout and the editor's current image all used to stop
    // following the image once the user reached for the wheel instead of the buttons.
    stack.onImageChange?.(afterScroll);

    observeSize(plan.viewport, () => stack.resize());
    report(`mounted ${records.length} image(s).`);
    return editor ? { ...stack, editor } : stack;
}

/**
 * Build the tooth editor and wire its controls.
 *
 * Separate from the main bootstrap because it is a whole feature rather than a branch, and
 * because the intraoral surface is the only caller: keeping it here means teleradiography
 * loads the code and never runs it, which is the cost of one bundle for both surfaces and
 * is cheaper than a second bundle that duplicates the stack.
 *
 * @returns {Promise<object|null>} the editor, or null if its controls are absent.
 */
async function mountSegmentation({
    doc,
    mounted,
    stack,
    records,
    data,
    prefix,
    origin,
    report,
    notify,
}) {
    const segPlan = segmentationControlPlan(doc);
    if (!segPlan.teeth) {
        report(`no #${SEGMENTATION_CONTROL_IDS.teeth} on this page; segmentation is off.`);
        return null;
    }
    if (!mounted.segmentation) {
        report('the entry supplied no segmentation bindings; segmentation is off.');
        return null;
    }

    const url = (suffix) =>
        new URL(`${prefix}/patients/${data.patientId}/tooth-segmentation${suffix}`, origin).href;

    const editor = createToothEditor({
        stack,
        plan: segPlan,
        toolName: mounted.segmentation.toolName,
        endpoints: { state: url('/state/'), save: url('/') },
        cornerstone: mounted.segmentation,
        io: {
            fetchImpl: (input, init) =>
                fetch(input, { credentials: 'same-origin', ...(init ?? {}) }),
            csrfToken: () => csrfToken(doc),
        },
        canModify: Boolean(data.canModify),
        report: notify,
    });

    editor.setImages(records);
    try {
        await editor.load();
    } catch (error) {
        // A study whose polygons cannot be fetched is still worth showing, and the grid
        // still works -- what must not happen is drawing on top of state we failed to read
        // and then saving over it, so the editor stays in its empty state and says so.
        report(`could not read the tooth segmentation: ${error.message}`);
        notify?.('danger', 'The tooth segmentation could not be loaded.');
        return editor;
    }
    await editor.showImage(records[0].fileId, { force: true });

    // The mode switch, read back out of the DOM exactly as the measurement one is -- a
    // captured boolean is how the grid's switch came to invert itself after one click.
    segPlan.mode?.addEventListener?.('click', () => {
        const enabled = segPlan.mode.getAttribute('aria-checked') !== 'true';
        segPlan.mode.setAttribute('aria-checked', enabled ? 'true' : 'false');
        const state = segPlan.mode.querySelector?.('[data-mode-state]');
        if (state) {
            state.textContent = enabled ? 'on' : 'off';
        }
        editor.setMode(enabled);
    });
    // Off to begin with, outlines hidden -- the same shape as the Measure switch, which
    // hides its measurements when it is off. `setMode(false)` leaves the tool *passive*
    // rather than disabled, so the outlines are ready to render the moment it goes on.
    editor.setMode(false);

    report(`tooth segmentation ready for ${records.length} image(s).`);
    return editor;
}

/**
 * The two endpoints of the most recent Length annotation, in image pixels.
 *
 * Calibration reuses the Length tool rather than adding a bespoke one: the user has
 * already learned to draw a line, and a second line-drawing interaction that looked the
 * same but behaved differently would be worse than reusing the first.
 */
export function pendingCalibrationLine(stack, worldToImage, imageId) {
    const lengths = stack
        .readAnnotations()
        .filter((entry) => entry?.metadata?.toolName === 'Length')
        .filter((entry) => (entry?.data?.handles?.points ?? []).length === 2);
    const last = lengths[lengths.length - 1];
    if (!last) {
        return null;
    }
    // Converted to pixels here, not sent as world coordinates: the endpoint derives
    // millimetres *per pixel*, so a world-space distance would produce a scale wrong by
    // whatever the current spacing is -- and on an already-calibrated image that is not 1.
    const [a, b] = last.data.handles.points.map((point) => worldToImage(imageId, point));
    const pointA = [Number(a[0]), Number(a[1])];
    const pointB = [Number(b[0]), Number(b[1])];
    return {
        pointA,
        pointB,
        pixelDistance: Math.hypot(pointB[0] - pointA[0], pointB[1] - pointA[1]),
    };
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
