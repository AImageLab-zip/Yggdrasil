/**
 * World coordinates in, image pixels out -- and back.
 *
 * ## The bug this exists to fix
 *
 * A `StackViewport` is 2D on screen and **not** 2D in its data. Cornerstone puts every
 * annotation handle in world space, three ordinates, for a photograph exactly as it does
 * for a volume. The first version of this surface declared `image_pixel` and then sent the
 * handles through untouched, so the server -- which correctly refuses a three-ordinate
 * handle in a two-dimensional frame -- rejected every single save with
 * "a handle in this frame must be 2 coordinates".
 *
 * The declaration was the honest part; the conversion was missing. `image_pixel` means
 * pixels, so pixels are what must be sent, and this is where world becomes pixels.
 *
 * ## Why Cornerstone's own converters, injected
 *
 * `core/utilities/worldToImageCoords` and `imageToWorldCoords` are exact inverses of each
 * other and are derived from the same `imagePlaneModule` the viewport renders from. Doing
 * the arithmetic here instead would be a third implementation of the mapping, and the one
 * most likely to disagree -- silently, by half a pixel, because both upstream functions
 * offset by half a spacing and it is easy not to notice.
 *
 * They are injected rather than imported so this module stays testable without a
 * `metaData` registry: both call `metaData.get('imagePlaneModule', imageId)` internally.
 *
 * ## The ordering, which is not the obvious one
 *
 * `worldToImageCoords` returns `[rowDistance / rowPixelSpacing, columnDistance /
 * columnPixelSpacing]`, and with this surface's `rowCosines = [1, 0, 0]` /
 * `columnCosines = [0, 1, 0]` that is `[x, y]` -- which is what the legacy tooth
 * polygons are in and what `image_pixel` means everywhere else in this codebase. Change
 * the cosines in the metadata provider and that stops being true, so the two modules are
 * coupled and the test for this one says so.
 */

/** How far off the image plane a handle may be before it is a bug rather than rounding. */
export const PLANE_TOLERANCE_MM = 1e-6;

/**
 * Convert one annotation's handles from world space to image pixels.
 *
 * @param {object} annotation a Cornerstone annotation.
 * @param {string} imageId the image its handles are on.
 * @param {Function} worldToImage `(imageId, worldPoint) => [x, y]`.
 * @returns {object} a copy whose `data.handles.points` are two-ordinate.
 */
export function annotationToImagePixels(annotation, imageId, worldToImage) {
    const points = annotation?.data?.handles?.points;
    if (!Array.isArray(points)) {
        throw new Error('An annotation without handles cannot be converted.');
    }
    return {
        ...annotation,
        data: {
            ...annotation.data,
            handles: {
                ...annotation.data.handles,
                points: points.map((point) => {
                    const converted = worldToImage(imageId, point);
                    return [Number(converted[0]), Number(converted[1])];
                }),
            },
        },
    };
}

/**
 * Convert stored image-pixel handles back to world space, for restoring.
 *
 * @param {object} annotation as stored, two-ordinate handles.
 * @param {string} imageId the image to place it on.
 * @param {Function} imageToWorld `(imageId, [x, y]) => [x, y, z]`.
 * @returns {object} a copy Cornerstone can render.
 */
export function annotationToWorld(annotation, imageId, imageToWorld) {
    const points = annotation?.data?.handles?.points;
    if (!Array.isArray(points)) {
        throw new Error('A stored annotation without handles cannot be restored.');
    }
    return {
        ...annotation,
        data: {
            ...annotation.data,
            handles: {
                ...annotation.data.handles,
                points: points.map((point) => {
                    // Already three ordinates: a payload written by an older client, or
                    // one that never went through the conversion. Passed through rather
                    // than re-converted, because converting a world point as though it
                    // were pixels would move it a long way and look plausible.
                    if (point.length === 3) {
                        return [...point];
                    }
                    return Array.from(imageToWorld(imageId, [point[0], point[1]]));
                }),
            },
        },
    };
}

/**
 * Assert a round trip is lossless for one image, and say by how much if it is not.
 *
 * Called once per stack on mount. The two converters are inverses *by construction*, so
 * this is not checking arithmetic -- it is checking that the metadata provider gave them
 * a plane module they can both work from. A missing `imagePositionPatient` or a cosine
 * pair that is not orthonormal produces a mapping that is silently wrong in a way no
 * measurement would reveal until somebody compared two of them.
 *
 * @returns {{ok: boolean, deviation: number}}
 */
export function checkRoundTrip(imageId, { worldToImage, imageToWorld, probe = [10, 20] }) {
    const world = imageToWorld(imageId, probe);
    const back = worldToImage(imageId, world);
    const deviation = Math.max(
        Math.abs(Number(back[0]) - probe[0]),
        Math.abs(Number(back[1]) - probe[1])
    );
    return { ok: deviation <= 1e-6, deviation };
}
