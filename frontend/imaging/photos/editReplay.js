/**
 * Re-projecting stored polygons through the image edits applied under them.
 *
 * `static/js/modality_viewers/rgb_editor.js` is a *geometric* editor -- crop, mirror,
 * rotate -- and it writes a new `FileRegistry` row with the operation list in
 * `metadata['edit_meta']`. Every tooth polygon already drawn on that photograph is
 * expressed in the *old* pixel frame, so without this the segmentation silently detaches
 * from the anatomy the first time somebody straightens a picture.
 *
 * Ported from `static/js/intraoral_segmentation.js:445-495`, which the Phase 4+5 work
 * deletes. Two things about that port are deliberate.
 *
 * **This is not undo/redo.** The file it came from has both, next to each other, and
 * they are unrelated: `annotations/history.js` is the user's edit log, and this is a
 * coordinate re-projection driven by what the *image editor* did. Conflating them is the
 * biggest hazard in this migration and is why they are separate modules.
 *
 * **The server has the same algorithm, and it had drifted.**
 * `maxillo/views/intraoral_segmentation.py` implements the read path and was missing
 * `rotate-cw` and `rotate-arbitrary` entirely, so a rotated photograph read back
 * *untransformed* source polygons -- a live bug, not a porting concern. Both
 * implementations are now driven by the same fixture file,
 * `common/fixtures/image_edit_replay.json`, so they cannot drift again without a test
 * failing on both sides.
 *
 * Replay is always **from the pristine geometry**, never composed onto already-
 * transformed points: `transformTeeth(baseTeeth, editMeta)` is idempotent, and a preview
 * that accumulated would drift a little further with every keystroke.
 */

/** Coordinates are rounded to this many decimals, matching the server exactly. */
const PRECISION = 3;

function round(value) {
    return Number(value.toFixed(PRECISION));
}

/**
 * One point through one operation.
 *
 * An unknown operation type is the identity rather than an error: `edit_meta` is written
 * by the editor and read here, and a viewer that threw on an operation it did not
 * recognise would make an image unopenable rather than merely unrotated. The server
 * makes the same choice.
 *
 * @param {number[]} point `[x, y]`
 * @param {object} operation
 * @returns {number[]}
 */
export function applyOperation(point, operation) {
    const x = Number(point[0]);
    const y = Number(point[1]);

    switch (operation?.type) {
        case 'flip-h':
            return [round(Number(operation.input_width || 0) - x), round(y)];
        case 'flip-v':
            return [round(x), round(Number(operation.input_height || 0) - y)];
        case 'crop':
            return [round(x - Number(operation.x || 0)), round(y - Number(operation.y || 0))];
        case 'rotate-cw':
            // A quarter turn clockwise: the new x is measured down the old y axis from
            // the bottom, and the new y is the old x.
            return [round(Number(operation.input_height || 0) - y), round(x)];
        case 'rotate-arbitrary': {
            const theta = ((((Number(operation.angle) || 0) % 360) + 360) % 360) * (Math.PI / 180);
            const cx = Number(operation.input_width || 0) / 2;
            const cy = Number(operation.input_height || 0) / 2;
            const dx = x - cx;
            const dy = y - cy;
            // The sign convention is the editor's, not a mathematical choice: it rotates
            // about the image centre into a bounding box whose origin the crop then
            // moves. Changing either term rotates the polygons the other way from the
            // pixels, which looks like a plausible segmentation of the wrong teeth.
            return [
                round(
                    dx * Math.cos(theta) +
                        dy * Math.sin(theta) +
                        Number(operation.bb_width || 0) / 2 -
                        Number(operation.crop_x || 0)
                ),
                round(
                    -dx * Math.sin(theta) +
                        dy * Math.cos(theta) +
                        Number(operation.bb_height || 0) / 2 -
                        Number(operation.crop_y || 0)
                ),
            ];
        }
        default:
            return [round(x), round(y)];
    }
}

/**
 * Sutherland--Hodgman clip of a polygon against an axis-aligned rectangle.
 *
 * A crop is not a coordinate shift for a polygon that crosses the new edge: the part
 * outside is gone, and the ring has to close along the cut. Translating without clipping
 * would leave vertices at negative coordinates, which render outside the image and read
 * as a corrupt segmentation.
 *
 * @param {number[][]} polygon
 * @param {{left: number, top: number, right: number, bottom: number}} rect
 * @returns {number[][]} empty when fewer than three vertices survive.
 */
export function clipPolygonToRect(polygon, { left, top, right, bottom }) {
    const inside = (point, edge) => {
        if (edge === 'left') return point[0] >= left;
        if (edge === 'right') return point[0] <= right;
        if (edge === 'top') return point[1] >= top;
        return point[1] <= bottom;
    };

    const intersect = (start, end, edge) => {
        const [x1, y1] = start;
        const [x2, y2] = end;
        if (edge === 'left' || edge === 'right') {
            const x = edge === 'left' ? left : right;
            const dx = x2 - x1;
            // A segment parallel to the edge has no crossing; taking the endpoint's
            // ordinate is what the server does and keeps the two byte-identical.
            if (Math.abs(dx) < 1e-9) return [x, y1];
            return [x, y1 + (y2 - y1) * ((x - x1) / dx)];
        }
        const y = edge === 'top' ? top : bottom;
        const dy = y2 - y1;
        if (Math.abs(dy) < 1e-9) return [x1, y];
        return [x1 + (x2 - x1) * ((y - y1) / dy), y];
    };

    let result = polygon.map((point) => [Number(point[0]), Number(point[1])]);
    for (const edge of ['left', 'right', 'top', 'bottom']) {
        const output = [];
        for (let index = 0; index < result.length; index += 1) {
            const current = result[index];
            const previous = result[(index - 1 + result.length) % result.length];
            const currentInside = inside(current, edge);
            const previousInside = inside(previous, edge);
            if (currentInside) {
                if (!previousInside) output.push(intersect(previous, current, edge));
                output.push(current);
            } else if (previousInside) {
                output.push(intersect(previous, current, edge));
            }
        }
        result = output;
        if (result.length < 3) return [];
    }
    return result.map((point) => [round(point[0]), round(point[1])]);
}

/**
 * One polygon through an ordered operation list.
 *
 * @param {number[][]} polygon
 * @param {object[]} operations
 * @returns {number[][]} empty when the shape does not survive, which is a real outcome:
 *   a crop can remove a tooth from the picture entirely.
 */
export function transformPolygon(polygon, operations) {
    let next = polygon.map((point) => [Number(point[0]), Number(point[1])]);
    for (const operation of operations || []) {
        if (!operation?.type || next.length < 3) return [];
        if (operation.type === 'crop') {
            next = clipPolygonToRect(next, {
                left: Number(operation.x || 0),
                top: Number(operation.y || 0),
                right: Number(operation.x || 0) + Number(operation.width || 0),
                bottom: Number(operation.y || 0) + Number(operation.height || 0),
            });
            if (next.length < 3) return [];
        }
        next = next.map((point) => applyOperation(point, operation));
    }
    return next.length >= 3 ? next : [];
}

/**
 * A whole `{FDI: [[[x, y], ...], ...]}` map through an `edit_meta`.
 *
 * Always call this with the *pristine* geometry. It is idempotent by construction only
 * because it never composes onto its own output.
 *
 * @param {object} teeth
 * @param {object} editMeta `{operations: [...]}`, or anything falsy for "no edits".
 * @returns {object} a new map; teeth whose polygons all vanish are dropped.
 */
export function transformTeeth(teeth, editMeta) {
    const operations = Array.isArray(editMeta?.operations) ? editMeta.operations : [];
    const out = {};
    for (const [tooth, polygons] of Object.entries(teeth || {})) {
        const transformed = (Array.isArray(polygons) ? polygons : [])
            .map((polygon) =>
                operations.length
                    ? transformPolygon(polygon, operations)
                    : polygon.map((point) => [Number(point[0]), Number(point[1])])
            )
            .filter((polygon) => polygon.length >= 3);
        if (transformed.length) out[String(tooth)] = transformed;
    }
    return out;
}
