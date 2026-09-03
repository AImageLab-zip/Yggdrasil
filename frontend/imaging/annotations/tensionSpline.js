/**
 * The curve a tooth polygon is drawn as.
 *
 * ## What this is for
 *
 * The old editor drew each tooth ring as a Konva `Line` with `tension: 0.35`, so **the
 * shape on screen was never the shape in the database**: the stored polygon is the vertex
 * list, and the drawn curve is a spline through it. 5,491 stored segmentations were
 * approved by looking at that curve.
 *
 * The roadmap says to decide `tension: 0.35` deliberately rather than discover it on a
 * clinician's screen. This module is that decision: **reproduce the curve exactly, in
 * rendering only, and leave the stored points alone.** The control points a
 * `SplineROITool` annotation holds *are* the polygon, so `annotations_crosscheck` keeps
 * comparing like with like and every existing study renders as it always has.
 *
 * ## Why not Cornerstone's own CardinalSpline
 *
 * Konva's `tension` is a *chord-length-weighted* cardinal spline: each vertex's control
 * points are offset along `P₊₁ − P₋₁` by `tension · d/(d₀₁ + d₁₂)`, so a short segment
 * pulls less than a long one. Cornerstone's `CardinalSpline` is *uniform* -- its transform
 * matrix applies `scale` regardless of segment length, which a constant matrix has no way
 * to avoid.
 *
 * They agree only where vertices are evenly spaced. Hand-drawn tooth rings are not, and
 * the measured worst-case deviation on synthetic rings is:
 *
 * | candidate                              | 12 even | 12 uneven | 8 very uneven | 30 dense |
 * |----------------------------------------|---------|-----------|---------------|----------|
 * | straight polygon (`LinearSpline`)      |    3.97 |      5.28 |          7.70 |     1.48 |
 * | `CardinalSpline`, scale 0.5 (default)  |    0.73 |      2.89 |          3.53 |     1.08 |
 * | this module                            |   ~1e-12|    ~1e-12 |        ~1e-12 |   ~1e-12 |
 *
 * (image pixels; a 4 px error on a premolar ring is a visible change to a stored shape
 * nobody edited.)
 *
 * ## How it hooks in
 *
 * `SplineROITool._getSplineConfig(type)` is a plain lookup in
 * `configuration.spline.configuration`, so a new type key with a `Class` is a supported
 * extension point rather than a patch. The class extends `CardinalSpline` rather than
 * `CubicSpline` for one concrete reason: `_updateSplineInstance` only pushes the
 * configured `scale` onto the instance when `spline instanceof CardinalSpline`, so
 * extending it keeps the tension a config value instead of a constant baked in here.
 *
 * Only `_getPoint` is overridden -- everything else (arc length, closest point, polyline
 * generation, AABB, control-point insertion at a `u`) is upstream's and stays upstream's.
 * That method is private by name, so `frontend/tests/tensionSpline.test.js` asserts the
 * hooks it relies on still exist on the installed package: a version bump that renames
 * them fails the build instead of silently rendering a different curve. Same guard the
 * vtk.js shader splice uses in `attenuatedMip.test.js`.
 *
 * The base class comes from the package's public `splines` export, not a deep path into
 * `dist/`, so the only unsupported thing here is the one override -- and that is what the
 * test watches.
 */

import { splines } from '@cornerstonejs/tools';

const { CardinalSpline } = splines;

/**
 * The tension the old editor used, and therefore the tension every stored polygon was
 * drawn and approved at. Changing it changes the appearance of 5,491 existing
 * segmentations; it is not a taste setting.
 */
export const TOOTH_TENSION = 0.35;

/** The `spline.type` key this class is registered under. */
export const KONVA_TENSION = 'KONVA_TENSION';

/**
 * Konva's control points for one vertex.
 *
 * @param {number[]} prev the previous vertex.
 * @param {number[]} point the vertex.
 * @param {number[]} next the following vertex.
 * @param {number} tension
 * @returns {{before: number[], after: number[]}} the incoming and outgoing Bézier handles.
 */
export function tensionControls(prev, point, next, tension) {
    const d01 = Math.hypot(point[0] - prev[0], point[1] - prev[1]);
    const d12 = Math.hypot(next[0] - point[0], next[1] - point[1]);
    const total = d01 + d12;
    // Coincident neighbours: the handles collapse onto the vertex, which degrades to a
    // straight segment rather than dividing by zero and producing NaN coordinates that
    // would propagate into the drawn polyline.
    if (!total) {
        return { before: [point[0], point[1]], after: [point[0], point[1]] };
    }
    const before = (tension * d01) / total;
    const after = (tension * d12) / total;
    const dx = next[0] - prev[0];
    const dy = next[1] - prev[1];
    return {
        before: [point[0] - before * dx, point[1] - before * dy],
        after: [point[0] + after * dx, point[1] + after * dy],
    };
}

/**
 * A point on a cubic Bézier.
 *
 * @param {number[]} start
 * @param {number[]} control1
 * @param {number[]} control2
 * @param {number[]} end
 * @param {number} t 0..1
 * @returns {number[]}
 */
export function cubicPoint(start, control1, control2, end, t) {
    const mt = 1 - t;
    const mt2 = mt * mt;
    const t2 = t * t;
    return [
        mt2 * mt * start[0] + 3 * mt2 * t * control1[0] + 3 * mt * t2 * control2[0] + t2 * t * end[0],
        mt2 * mt * start[1] + 3 * mt2 * t * control1[1] + 3 * mt * t2 * control2[1] + t2 * t * end[1],
    ];
}

/**
 * The reference curve, as the old editor drew it.
 *
 * Kept beside the class rather than in the test, because it is the specification: this is
 * `curveMidpoint`/`tensionControls`/`cubicPoint` from
 * `static/js/intraoral_segmentation.js:231-267`, which is what Konva's `tension` does for
 * a closed line. The test asserts the spline agrees with it; without both in the source
 * tree there is nothing to agree *with*.
 *
 * @param {number[][]} polygon closed ring of `[x, y]`.
 * @param {object} [options]
 * @param {number} [options.tension]
 * @param {number} [options.segments] samples per span.
 * @returns {number[][]} the drawn polyline.
 */
export function konvaTensionCurve(polygon, { tension = TOOTH_TENSION, segments = 40 } = {}) {
    const count = polygon.length;
    const points = [];
    for (let index = 0; index < count; index += 1) {
        const previous = polygon[(index - 1 + count) % count];
        const start = polygon[index];
        const end = polygon[(index + 1) % count];
        const following = polygon[(index + 2) % count];
        const control1 = tensionControls(previous, start, end, tension).after;
        const control2 = tensionControls(start, end, following, tension).before;
        for (let step = 0; step < segments; step += 1) {
            points.push(cubicPoint(start, control1, control2, end, step / segments));
        }
    }
    return points;
}

/**
 * `CardinalSpline`, re-weighted by chord length so it draws Konva's curve.
 *
 * Constructed with no arguments by `SplineROITool` (`new (...Class)()`), which is why the
 * tension defaults here and is then set from the tool's `scale` config.
 */
export class KonvaTensionSpline extends CardinalSpline {
    constructor(props = {}) {
        super({ ...props, scale: props.scale ?? TOOTH_TENSION });
    }

    /**
     * One point on the curve, at spline parameter `u`.
     *
     * Replaces the uniform matrix evaluation with Konva's chord-weighted Bézier. The
     * segment's four control points, the wrap-around for a closed ring and the mirroring
     * for an open one all come from upstream's `_getCurveSegmentPoints`, so an open
     * contour behaves exactly as any other spline here does.
     *
     * @param {number} u
     * @param {*} _transformMatrix unused -- the whole reason this override exists is that
     *   a constant matrix cannot express a chord-length weight.
     * @param {number[][]} [controlPoints]
     * @param {boolean} [closed]
     * @returns {number[]|undefined}
     */
    _getPoint(u, _transformMatrix, controlPoints = this.controlPoints, closed = this.closed) {
        const segmentCount = this._getNumCurveSegments(controlPoints, closed);
        if (!segmentCount) {
            return undefined;
        }
        const whole = Math.floor(u);
        let index = whole % segmentCount;
        const t = u - whole;
        if (index < 0) {
            if (!closed) {
                return undefined;
            }
            index = (segmentCount + index) % segmentCount;
        }
        const { p0, p1, p2, p3 } = this._getCurveSegmentPoints(index, controlPoints, closed);
        const tension = this.scale;
        return cubicPoint(
            p1,
            tensionControls(p0, p1, p2, tension).after,
            tensionControls(p1, p2, p3, tension).before,
            p2,
            t
        );
    }
}

/**
 * The `spline` block for a tool that should draw tooth rings.
 *
 * `resolution` is samples-per-span minus one, upstream's convention. 19 gives 20 samples,
 * which is upstream's own default and is smooth at the zoom levels a photograph is edited
 * at; raising it costs a longer polyline on every render for no visible gain.
 *
 * @returns {object} merge into a `SplineROITool` configuration.
 */
export function toothSplineConfiguration() {
    return {
        type: KONVA_TENSION,
        configuration: {
            [KONVA_TENSION]: {
                Class: KonvaTensionSpline,
                scale: TOOTH_TENSION,
                resolution: 19,
                // Shift-click adds a vertex on the curve, Ctrl-click removes one -- the
                // midpoint handles and vertex deletion the old editor hand-rolled, and the
                // reason `curveMidpoint` existed at all was to place those handles *on*
                // the drawn curve. Upstream inserts at the closest point's `u`, which is
                // on the curve by construction.
                controlPointAdditionEnabled: true,
                controlPointDeletionEnabled: true,
            },
        },
    };
}
