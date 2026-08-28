/**
 * Between Cornerstone's contours and Yggdrasil's `{FDI: [[[x, y], …], …]}` map.
 *
 * Pure, and the pure half is the half worth testing: the coordinate transforms are handed
 * in, so this file has no Cornerstone import and no GPU, and what it asserts is the
 * property the cross-check depends on -- that a polygon drawn in the browser arrives at
 * `annotations/adapters/tooth_segmentation.py` in the *same shape and order* the legacy
 * converter produces.
 *
 * ## Why the FDI code is the annotation's `label`
 *
 * A contour has no idea which tooth it is. The code has to be attached the moment the user
 * draws it and has to survive being read back, and `data.label` is the annotation's own
 * field for exactly that -- no namespaced key invented, and it is the only place the tool's
 * `getTextLines(data, targetId)` can see it, because that callback is handed `data` and
 * never the metadata.
 *
 * It is never *read* from Cornerstone as truth. The server resolves the code against the
 * seeded `fdi-permanent` vocabulary and refuses an unknown one, which is where an
 * unlabelled polygon is caught rather than exported under the wrong segment.
 *
 * ## Ordering is the contract
 *
 * `tooth_polygons` orders by FDI code and then by polygon index, so two conversions of one
 * study are byte-identical. {@link teethFromAnnotations} sorts by code and keeps encounter
 * order within a code, which is the same order the old editor's arrays had. Anything else
 * would make `annotations_crosscheck` report drift on every study anybody edited -- the
 * signal it exists to give, buried in noise.
 */

import { isFdiCode } from './labelMapper.js';

/** Cornerstone refuses a spline with fewer control points, and so does a polygon. */
export const MIN_VERTICES = 3;

/**
 * The FDI code an annotation claims, or `null`.
 *
 * `null` for a well-formed contour that simply has not been assigned yet, and `null` for a
 * label that is not an FDI code -- both are "this cannot be stored", which is the only
 * distinction the callers make.
 *
 * @param {object} annotation
 * @returns {string|null}
 */
export function fdiOf(annotation) {
    const code = annotation?.data?.label;
    return isFdiCode(code) ? code : null;
}

/**
 * Attach an FDI code to an annotation.
 *
 * A one-line setter so the field is named in one place; every caller that assigned
 * `data.label` directly would be a place to update if the field ever moves.
 *
 * @param {object} annotation
 * @param {string} code
 */
export function setFdi(annotation, code) {
    if (!annotation.data) {
        annotation.data = {};
    }
    annotation.data.label = code;
}

/**
 * Round a stored coordinate the way the editor being replaced did.
 *
 * Three decimals, from `clampOriginalPoint`. Not cosmetic: without it a drag writes a
 * float with 17 significant digits, every autosave sends a different number for a polygon
 * nobody moved, and the confirmed-image comparison on the server sees a change.
 *
 * @param {number} value
 * @returns {number}
 */
export function roundCoordinate(value) {
    return Number(Number(value).toFixed(3));
}

/**
 * Clamp a point into the image and round it.
 *
 * A contour can be dragged past the edge of a photograph, and a vertex outside it is a
 * coordinate that cannot be re-projected when the image is cropped -- `editReplay.js`
 * would carry it somewhere meaningless. Clamping at the point of capture keeps every
 * stored vertex inside the bytes it was drawn on.
 *
 * @param {number[]} point `[x, y]` in image pixels.
 * @param {object} [bounds] `{width, height}`; omitted leaves the point unclamped.
 * @returns {number[]}
 */
export function clampToImage(point, bounds) {
    const [x, y] = point;
    if (!bounds?.width || !bounds?.height) {
        return [roundCoordinate(x), roundCoordinate(y)];
    }
    return [
        roundCoordinate(Math.min(Math.max(x, 0), bounds.width)),
        roundCoordinate(Math.min(Math.max(y, 0), bounds.height)),
    ];
}

/**
 * Cornerstone's contours for one image, as a teeth map.
 *
 * @param {Array<object>} annotations every annotation Cornerstone holds.
 * @param {object} options
 * @param {string} options.imageId only annotations referencing this image are read.
 * @param {(imageId: string, world: number[]) => number[]} options.worldToImage
 * @param {string} options.toolName the contour tool's registered name.
 * @param {object} [options.bounds] `{width, height}` of the image, for clamping.
 * @returns {object} `{FDI: [[[x, y], …], …]}`, ordered by code then encounter.
 */
export function teethFromAnnotations(annotations, { imageId, worldToImage, toolName, bounds }) {
    const byCode = new Map();
    for (const annotation of annotations ?? []) {
        if (annotation?.metadata?.toolName !== toolName) {
            continue;
        }
        if (annotation?.metadata?.referencedImageId !== imageId) {
            continue;
        }
        const code = fdiOf(annotation);
        if (!code) {
            // A contour with no tooth is not storable -- the server would refuse the whole
            // save. Dropping it here would lose the user's work silently, so the state
            // machine checks for these before saving and says so; this function simply
            // does not invent a code.
            continue;
        }
        const points = (annotation.data?.handles?.points ?? []).map((world) =>
            clampToImage(worldToImage(imageId, world), bounds)
        );
        if (points.length < MIN_VERTICES) {
            continue;
        }
        if (!byCode.has(code)) {
            byCode.set(code, []);
        }
        byCode.get(code).push(points);
    }

    const teeth = {};
    for (const code of [...byCode.keys()].sort()) {
        teeth[code] = byCode.get(code);
    }
    return teeth;
}

/**
 * Contours with no tooth assigned, for the caller to complain about.
 *
 * Separate from {@link teethFromAnnotations} because the two answers are needed at
 * different times: the map is needed to save, and this is needed to decide whether saving
 * is honest. A drawn shape that silently never persists is the worst outcome available.
 *
 * @param {Array<object>} annotations
 * @param {object} options
 * @param {string} options.imageId
 * @param {string} options.toolName
 * @returns {Array<object>}
 */
export function unassignedOutlines(annotations, { imageId, toolName }) {
    return (annotations ?? []).filter(
        (annotation) =>
            annotation?.metadata?.toolName === toolName &&
            annotation?.metadata?.referencedImageId === imageId &&
            !fdiOf(annotation)
    );
}

/**
 * A teeth map as one entry per polygon, ready for the viewport to draw.
 *
 * @param {object} teeth `{FDI: [[[x, y], …], …]}`
 * @param {object} options
 * @param {string} options.imageId
 * @param {(imageId: string, point: number[]) => number[]} options.imageToWorld
 * @returns {Array<{fdi: string, polygonIndex: number, points: number[][]}>} ordered by
 *   code then polygon index, so a restore draws them in the order they are stored.
 */
export function outlinesToDraw(teeth, { imageId, imageToWorld }) {
    const outlines = [];
    for (const code of Object.keys(teeth ?? {}).sort()) {
        const polygons = teeth[code];
        if (!Array.isArray(polygons)) {
            continue;
        }
        polygons.forEach((polygon, polygonIndex) => {
            if (!Array.isArray(polygon) || polygon.length < MIN_VERTICES) {
                // Degenerate rings exist in the legacy corpus and the adapter refuses
                // them. Drawing one would give the user a shape they cannot save.
                return;
            }
            outlines.push({
                fdi: code,
                polygonIndex,
                points: polygon.map((point) => imageToWorld(imageId, [point[0], point[1]])),
            });
        });
    }
    return outlines;
}

/**
 * Is what is on screen different from what was loaded?
 *
 * Compared on the rounded map rather than on annotation objects: Cornerstone rewrites
 * `cachedStats`, `invalidated` and handle sub-objects constantly, so an object comparison
 * would report a change on every render and the autosave would never stop.
 *
 * @param {object} left
 * @param {object} right
 * @returns {boolean}
 */
export function teethDiffer(left, right) {
    return JSON.stringify(normalizeTeeth(left)) !== JSON.stringify(normalizeTeeth(right));
}

/**
 * One comparable form of a teeth map: sorted codes, rounded coordinates.
 *
 * @param {object} teeth
 * @returns {object}
 */
export function normalizeTeeth(teeth) {
    const out = {};
    for (const code of Object.keys(teeth ?? {}).sort()) {
        const polygons = teeth[code];
        if (!Array.isArray(polygons) || !polygons.length) {
            continue;
        }
        const rings = polygons
            .filter((polygon) => Array.isArray(polygon) && polygon.length >= MIN_VERTICES)
            .map((polygon) => polygon.map((point) => [roundCoordinate(point[0]), roundCoordinate(point[1])]));
        if (rings.length) {
            out[code] = rings;
        }
    }
    return out;
}
