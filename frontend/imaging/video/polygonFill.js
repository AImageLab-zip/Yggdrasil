/**
 * Burn a freehand outline into a labelmap plane.
 *
 * ## Why this exists rather than a Cornerstone contour segmentation
 *
 * The polygon button used to activate `PlanarFreehandContourSegmentationTool`, and every
 * stroke threw. That tool is a *contour segmentation* tool: its
 * `createAnnotation` demands `getActiveSegmentation(viewportId).representationData.Contour`
 * (`tools/base/ContourSegmentationBaseTool.js:69-74`) and this surface has never created
 * anything but labelmaps, so the throw was unconditional -- swallowed by
 * `mouseEventHandlers/mouseDownActivate.js:21`, which is why the stroke was visible while
 * the mouse was down and simply gone on release, with a console line and no banner.
 *
 * Two ways out, and the choice is not close:
 *
 *  1. Give every region a Contour representation beside its Labelmap one. That satisfies
 *     the tool, and then the contour is a **second representation of the same fact**: it
 *     renders as SVG over the mask, it is not what `masks.js` saves, and something has to
 *     keep the two in step for the rest of the surface's life. `save_video_regions`
 *     already refuses that trade in as many words -- "a second, non-canonical copy of
 *     viewer state would be a thing to keep in step with the only thing that is true".
 *  2. Draw with `PlanarFreehandROITool`, which is the same freehand interaction and is
 *     *not* a contour segmentation tool (`isContourSegmentationTool()` returns false), and
 *     rasterise the finished outline into the region's labelmap. The polyline is consumed
 *     and the annotation is dropped, so the labelmap stays the only record.
 *
 * This module is the rasteriser for (2). Upstream's own
 * `LabelmapBaseTool.viewportContoursToLabelmap` was the third candidate and cannot be used
 * here: it reaches for `viewport.getDefaultActor().actor.getMapper().getInputData()` and
 * calls `worldToIndex` on the result, but a `VideoViewport`'s actors are `CanvasActor`s
 * whose `CanvasMapper.getInputData()` returns a Cornerstone *image*, which has no such
 * method.
 *
 * ## The fill
 *
 * Even-odd scanline, sampled at pixel centres. A freehand outline is closed implicitly by
 * the segment from its last point back to its first, which is what the tool draws when the
 * contour is closed and the honest reading of an open one: the user was circling a region,
 * not measuring a line.
 */

/**
 * Fill a closed polygon into a single-channel plane, in place.
 *
 * @param {object} options
 * @param {ArrayLike<number>} options.plane `width * height`, row-major, mutated.
 * @param {number} options.width
 * @param {number} options.height
 * @param {number[][]} options.points the outline in **pixel** coordinates, any winding.
 * @param {number} [options.value] what to write inside the outline; 0 erases.
 * @returns {number} how many pixels the fill touched, so a caller can tell a real stroke
 *   from a stray click without walking the plane again.
 */
export function fillPolygon({ plane, width, height, points, value = 1 }) {
    if (!plane || !(width > 0) || !(height > 0) || !Array.isArray(points) || points.length < 3) {
        return 0;
    }

    let top = Infinity;
    let bottom = -Infinity;
    for (const [, y] of points) {
        if (!Number.isFinite(y)) {
            return 0;
        }
        top = Math.min(top, y);
        bottom = Math.max(bottom, y);
    }
    const firstRow = Math.max(0, Math.ceil(top - 0.5));
    const lastRow = Math.min(height - 1, Math.floor(bottom - 0.5));

    let touched = 0;
    const crossings = [];
    for (let row = firstRow; row <= lastRow; row += 1) {
        // The scanline runs through the middle of the row, so a horizontal polygon edge can
        // never lie on it and the half-open comparison below cannot double-count a vertex.
        const scan = row + 0.5;
        crossings.length = 0;
        for (let index = 0; index < points.length; index += 1) {
            const [x1, y1] = points[index];
            const [x2, y2] = points[(index + 1) % points.length];
            if (y1 === y2) {
                continue;
            }
            // Half-open in y: a vertex shared by two edges is counted once, which is what
            // keeps a spike from leaving an unfilled column under it.
            if (scan >= Math.min(y1, y2) && scan < Math.max(y1, y2)) {
                crossings.push(x1 + ((scan - y1) / (y2 - y1)) * (x2 - x1));
            }
        }
        if (crossings.length < 2) {
            continue;
        }
        crossings.sort((a, b) => a - b);
        const rowOffset = row * width;
        for (let pair = 0; pair + 1 < crossings.length; pair += 2) {
            const from = Math.max(0, Math.ceil(crossings[pair] - 0.5));
            const to = Math.min(width - 1, Math.floor(crossings[pair + 1] - 0.5));
            for (let column = from; column <= to; column += 1) {
                const offset = rowOffset + column;
                if (plane[offset] !== value) {
                    plane[offset] = value;
                    touched += 1;
                }
            }
        }
    }
    return touched;
}

/**
 * A world-space outline in the pixel coordinates the labelmap plane is indexed by.
 *
 * `viewport.getImageData().imageData.worldToIndex` is the supported accessor and the only
 * one a `VideoViewport` answers: it composes `worldToCanvas` with `canvasToIndex`, so it
 * follows the pan and zoom the reader drew under (`VideoViewport.js:449-467`).
 *
 * @param {object} viewport
 * @param {number[][]} polyline world points, as `annotation.data.contour.polyline` holds.
 * @returns {number[][]|null} pixel points, or null when the viewport cannot answer.
 */
export function polylineToPixels(viewport, polyline) {
    const imageData = viewport?.getImageData?.()?.imageData;
    if (typeof imageData?.worldToIndex !== 'function' || !Array.isArray(polyline)) {
        return null;
    }
    const points = [];
    for (const world of polyline) {
        const [x, y] = imageData.worldToIndex(world);
        if (!Number.isFinite(x) || !Number.isFinite(y)) {
            return null;
        }
        points.push([x, y]);
    }
    return points;
}
