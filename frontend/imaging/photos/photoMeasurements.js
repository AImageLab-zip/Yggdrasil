/**
 * Grouping a photo stack's annotations by the image they were drawn on, and building
 * the save.
 *
 * Pure. The `fetch` is one line at the call site, for the same reason it is in
 * `grid/measurements.js`: the interesting failures are all in *what is sent*, and none
 * of them are visible in a browser until months later.
 *
 * ## Why grouping exists at all
 *
 * An `AnnotationSet` is per patient, and a revision replaces the whole set. A stack has
 * N images, so a save that named only the image on screen would drop the others from the
 * new revision -- indistinguishable, afterwards, from the user having deleted them. The
 * server's answer is `save_measurement_groups`, which takes one group per resource and
 * carries forward the resources a save does not name; this builds those groups.
 *
 * Cornerstone already holds the whole stack's annotations at once (they are keyed by
 * `referencedImageId`), so sending them all is the natural shape rather than an
 * optimisation. The only work here is mapping an imageId back to the `FileRegistry` row
 * it came from, which the client knows and the server must not have to guess.
 *
 * ## The client sends no numbers
 *
 * Only geometry. The server recomputes every value from the handles
 * (`annotations/adapters/cornerstone.py`), so a length sent from here would be ignored
 * at best and believed at worst.
 *
 * ## It does send *pixels*, and that took a fix
 *
 * A `StackViewport` is 2D on screen and not in its data: Cornerstone puts every handle in
 * world space, three ordinates, for a photograph as for a volume. The first version
 * declared `image_pixel` and sent the handles untouched, so the server -- correctly
 * refusing a three-ordinate handle in a planar frame -- rejected every save. The
 * declaration was the honest part and the conversion was missing; `./coordinates.js` is
 * where world becomes pixels now, and `toImage` is required rather than optional so it
 * cannot be forgotten again.
 */

import {
    assertSavable,
    measurementAnnotations,
} from '../annotations/protocol.js';
import { annotationToImagePixels, annotationToWorld } from './coordinates.js';

/** The frame a stack viewport reports handles in. */
export const PHOTO_COORDINATE_SYSTEM = 'image_pixel';

/**
 * The image facts a saved measurement was measured against.
 *
 * Thin next to `volumeDescriptor`, and honestly so: a photograph has no affine, no
 * spacing that was not typed in by a person, and no orientation. What it does have is
 * dimensions, which is what makes "the same image, resized" detectable later.
 *
 * @param {object} record `{width, height, pixelSpacingMm}`.
 * @returns {object}
 */
export function imageDescriptor(record) {
    return {
        shape: [record.width, record.height],
        pixel_spacing_mm: record.pixelSpacingMm
            ? [record.pixelSpacingMm.x_mm, record.pixelSpacingMm.y_mm]
            : null,
        // Which client anchored these coordinates -- the first thing anybody asks when a
        // cross-check reports drift.
        recorded_by: 'photo-stack',
    };
}

/**
 * Split the viewer's annotations by the file each was drawn on.
 *
 * An annotation whose `referencedImageId` is not in the stack is **dropped rather than
 * assigned to the current image**. That happens when a stack is rebuilt after an image
 * edit, and guessing would move somebody's measurement onto a different photograph.
 *
 * @param {object[]} annotations everything `getAllAnnotations()` returned.
 * @param {Map<string, number>} imageIdToFileId
 * @returns {Map<number, object[]>} file id to its annotations, in encounter order.
 */
export function groupAnnotationsByFile(annotations, imageIdToFileId) {
    const grouped = new Map();
    for (const annotation of measurementAnnotations(annotations)) {
        const imageId = annotation?.metadata?.referencedImageId;
        const fileId = imageIdToFileId.get(imageId);
        if (fileId === undefined) {
            continue;
        }
        if (!grouped.has(fileId)) {
            grouped.set(fileId, []);
        }
        grouped.get(fileId).push(annotation);
    }
    return grouped;
}

/**
 * Build the POST body for a stack save.
 *
 * **Every image in the stack gets a group, including the empty ones.** That is the whole
 * point: an image with no entry would be carried forward by the server, so clearing an
 * image's measurements would silently restore them. An empty group is how a deletion is
 * expressed.
 *
 * @param {object} options
 * @param {object[]} options.images `{fileId, imageId, width, height, pixelSpacingMm}`.
 * @param {object[]} options.annotations the viewer's annotation list, verbatim.
 * @param {number} options.expectedRevision the revision the client loaded.
 * @param {Function} options.toImage `(imageId, worldPoint) => [x, y]`. **Required** --
 *   without it the handles go out in world space and the server refuses the whole save.
 * @returns {object} ready for `JSON.stringify`.
 */
export function buildStackSaveRequest({ images, annotations, expectedRevision, toImage }) {
    if (!Array.isArray(images) || images.length === 0) {
        throw new Error('A stack save must name at least one image.');
    }
    if (typeof toImage !== 'function') {
        throw new Error(
            'toImage is required: Cornerstone reports handles in world space, and a save ' +
                'declaring image_pixel must convert them or the server refuses all of it.'
        );
    }
    const imageIdToFileId = new Map(images.map((image) => [image.imageId, image.fileId]));
    const grouped = groupAnnotationsByFile(annotations, imageIdToFileId);

    const total = [...grouped.values()].reduce((sum, entries) => sum + entries.length, 0);
    // Checked against the whole save, matching the server: the cap guards against a
    // client resending its buffer, and a loop that did so would spread it over the group.
    assertSavable(new Array(total), expectedRevision);

    return {
        expectedRevision,
        coordinateSystem: PHOTO_COORDINATE_SYSTEM,
        images: images.map((image) => ({
            fileId: image.fileId,
            annotations: (grouped.get(image.fileId) ?? []).map((annotation) =>
                annotationToImagePixels(annotation, image.imageId, toImage)
            ),
            imageDescriptor: imageDescriptor(image),
        })),
    };
}

/**
 * The stored state, keyed by imageId, ready to hand back to the viewer.
 *
 * A file the server did not mention gets an empty list rather than being absent, so the
 * caller restores "nothing" explicitly instead of leaving whatever was on screen.
 *
 * @param {object[]} stateImages the `images` array from the state endpoint.
 * @param {Map<number, string>} fileIdToImageId
 * @param {Function} toWorld `(imageId, [x, y]) => [x, y, z]`; the inverse of the save's
 *   conversion, because stored handles are pixels and Cornerstone renders world space.
 * @returns {Map<string, object[]>}
 */
export function restorablesByImageId(stateImages, fileIdToImageId, toWorld) {
    const out = new Map();
    for (const [fileId, imageId] of fileIdToImageId) {
        const entry = (stateImages ?? []).find((candidate) => candidate?.fileId === fileId);
        const stored = Array.isArray(entry?.annotations) ? entry.annotations : [];
        out.set(
            imageId,
            typeof toWorld === 'function'
                ? stored
                      .filter((annotation) => Array.isArray(annotation?.data?.handles?.points))
                      .map((annotation) => annotationToWorld(annotation, imageId, toWorld))
                : stored
        );
    }
    return out;
}
