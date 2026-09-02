/**
 * The curve the dental arch is drawn as.
 *
 * ## Why a subclass rather than one of the five upstream splines
 *
 * The arch the baker fits is a **centripetal** Catmull-Rom through the control points:
 * `seg2pano_core.js:376-405`, with knots spaced by `pow(dx² + dy², 0.25)`. That is not a
 * style choice made here -- it is the curve `polynomialFromControlPoints` resamples before
 * fitting the degree-12 polynomial the projection actually follows, so a drawn curve that
 * is not it is a drawn curve that lies about where the strip will be cut.
 *
 * Cornerstone ships `CatmullRomSpline`, and it is the *uniform* one -- a `CardinalSpline`
 * with `scale: 0.5`, whose transform matrix cannot express a knot spacing that depends on
 * the distance between control points. The two agree only where the points are evenly
 * spaced. An arch's are not: `extractControlPoints` samples the fitted centreline at a
 * fixed *index* stride, and the centreline is sampled by arc length along a curve that
 * turns, so the spacing varies by a factor of two or more across the arch.
 *
 * Same situation as `annotations/tensionSpline.js`, and the same answer: a small subclass
 * overriding one private method, registered under its own spline type key, with a test
 * that pins both the curve *and* the upstream hooks the override stands on -- so a
 * Cornerstone bump that renames one fails the build instead of silently drawing a
 * different arch.
 *
 * ## What is reproduced, and what is deliberately not
 *
 * `_getPoint` evaluates the centripetal Catmull-Rom for the segment upstream asks about,
 * from the same `p0..p3` upstream picks. The result is the same *curve* as
 * `Seg2PanoCore.catmullRomChain`'s, sampled at a different parameter -- the chain walks
 * the knot interval directly, this walks `t` in `[0, 1]` and maps it onto that interval.
 * `archParity` beside the class is the reference the test compares against, kept in the
 * source tree because it is the specification.
 *
 * The chain covers only the segments between the second and second-to-last control point:
 * its first and last points are tangent handles, not arch. Upstream's open-spline
 * `_getCurveSegmentPoints` instead duplicates the endpoints, which would extend the drawn
 * arch past where the projection is defined. So the end segments are **clamped** rather
 * than extrapolated -- the arch stops where the baker's does.
 */

import { splines } from '@cornerstonejs/tools';

const { CardinalSpline } = splines;

/** The `spline.type` key this class is registered under. */
export const ARCH_SPLINE = 'YGG_ARCH';

/** The knot exponent that makes a Catmull-Rom centripetal (alpha = 0.5). */
export const KNOT_EXPONENT = 0.25;

/**
 * The knot spacing between two points, as `catmullRomSegment` computes it.
 *
 * @param {number[]} a
 * @param {number[]} b
 * @returns {number}
 */
export function knotStep(a, b) {
    const dx = b[0] - a[0];
    const dy = b[1] - a[1];
    return (dx * dx + dy * dy) ** KNOT_EXPONENT;
}

/** Linear interpolation between `a` and `b` over the knot interval `[ta, tb]`. */
function interpolatePoint(a, b, ta, tb, t) {
    if (tb === ta) {
        return [a[0], a[1]];
    }
    const left = (tb - t) / (tb - ta);
    const right = (t - ta) / (tb - ta);
    return [left * a[0] + right * b[0], left * a[1] + right * b[1]];
}

/**
 * One point of the centripetal Catmull-Rom curve through `p0..p3`.
 *
 * `u` runs 0..1 across the segment between `p1` and `p2`, which is the only part of the
 * four the curve is defined on -- the outer two are tangent handles.
 *
 * This is `catmullRomSegment`'s Barry-Goldman pyramid (`seg2pano_core.js:376-396`), point
 * by point rather than sampled into a list.
 *
 * @returns {number[]|null} null where the knots collapse, which is a degenerate segment
 *   rather than a NaN to propagate into the drawn polyline.
 */
export function centripetalPoint(p0, p1, p2, p3, u) {
    const t0 = 0;
    const t1 = t0 + knotStep(p0, p1);
    const t2 = t1 + knotStep(p1, p2);
    const t3 = t2 + knotStep(p2, p3);
    if (t1 === t0 || t2 === t1 || t3 === t2) {
        return null;
    }
    const t = t1 + u * (t2 - t1);
    const a1 = interpolatePoint(p0, p1, t0, t1, t);
    const a2 = interpolatePoint(p1, p2, t1, t2, t);
    const a3 = interpolatePoint(p2, p3, t2, t3, t);
    const b1 = interpolatePoint(a1, a2, t0, t2, t);
    const b2 = interpolatePoint(a2, a3, t1, t3, t);
    return interpolatePoint(b1, b2, t1, t2, t);
}

/**
 * The arch curve, as `SplineROITool` draws it.
 *
 * Only `_getPoint` is overridden. Arc length, closest-point, polyline generation, the AABB
 * and control-point insertion are upstream's and stay upstream's.
 *
 * **The base is `CardinalSpline`, not `CubicSpline`, and that is load-bearing even though
 * the transform matrix is unused here.** `getTransformMatrix` is abstract on `Spline` and
 * implemented only by `CardinalSpline`, `BSpline` and `QuadraticBezier`; `CubicSpline`
 * *calls* it -- unconditionally, before dispatching to `_getPoint`
 * (`splines/CubicSpline.js:30`, and again at `:12` and `:140`). Extending `CubicSpline`
 * therefore threw `this.getTransformMatrix is not a function` on the first render of every
 * arch, inside `renderAnnotationInstance` and before a single handle was drawn -- the
 * editor looked like it had no control points at all. `tensionSpline.js` extends
 * `CardinalSpline` for its own reason (`_updateSplineInstance` only pushes `scale` onto a
 * `CardinalSpline`); this class needs it simply to have the method its base class calls.
 *
 * `fixedScale` is asserted so `_updateSplineInstance` never writes a `scale` onto an
 * instance whose curve does not have one.
 */
export class ArchSpline extends CardinalSpline {
    constructor(props = {}) {
        super({ ...props, fixedScale: true });
    }

    /**
     * @param {number} u the spline parameter; its integer part is the segment index.
     * @param {number[]} transformMatrix unused -- a centripetal spline has no constant
     *   basis matrix, which is the whole reason this class exists.
     */
    _getPoint(u, transformMatrix, controlPoints = this.controlPoints, closed = this.closed) {
        const segments = this._getNumCurveSegments(controlPoints, closed);
        const index = Math.floor(u);
        if (index < 0 || index >= segments) {
            return closed
                ? super._getPoint(u, transformMatrix, controlPoints, closed)
                : undefined;
        }
        const { p0, p1, p2, p3 } = this._getCurveSegmentPoints(index, controlPoints, closed);
        // The first and last segments of an open arch have a duplicated endpoint standing
        // in for a tangent handle upstream invented. The baker's chain does not draw them
        // at all, so they are held straight rather than curved outward into arch the
        // projection has no polynomial for.
        const point = centripetalPoint(p0, p1, p2, p3, u - index);
        if (point) {
            return point;
        }
        // Collapsed knots: two coincident control points, which a user can produce by
        // dragging one onto another. A straight segment is the honest degradation.
        return interpolatePoint(p1, p2, 0, 1, u - index);
    }
}

/**
 * `SplineROITool`'s spline configuration for the arch.
 *
 * `_getSplineConfig(type)` is a plain lookup in `configuration.spline.configuration`, so a
 * new type key with a `Class` is a supported extension point rather than a patch.
 *
 * Only keys of `DEFAULT_SPLINE_CONFIG` mean anything here -- an invented one is merged in
 * and never read. In particular the arch being **open** (it runs condyle to condyle, and
 * joining its ends would draw a loop through the tongue) is not stated here: the switch is
 * the tool-level `allowOpenSplines`, set where the tool is added in `archViewport.js`.
 */
export function archSplineConfiguration() {
    return {
        type: ARCH_SPLINE,
        configuration: {
            [ARCH_SPLINE]: {
                Class: ArchSpline,
                controlPointAdditionEnabled: true,
                controlPointDeletionEnabled: true,
            },
        },
    };
}
