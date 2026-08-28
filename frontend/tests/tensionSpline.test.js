/**
 * The tooth-ring curve, and the upstream hooks it stands on.
 *
 * Two jobs, and the second matters as much as the first:
 *
 * 1. **Parity.** `KonvaTensionSpline` must draw exactly what the Konva editor drew, or
 *    5,491 stored segmentations change appearance without anybody editing them.
 * 2. **A version-bump tripwire.** The class overrides `_getPoint` and calls
 *    `_getCurveSegmentPoints` and `_getNumCurveSegments`, all private by name. If a
 *    Cornerstone bump renames one, the override silently stops being called and every
 *    tooth renders as a *uniform* cardinal spline -- a few pixels off, on every study,
 *    with nothing to say so. Reading the real installed class here makes that a build
 *    failure. Same reasoning as `attenuatedMip.test.js` reading the real vtk.js shader.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { splines } from '@cornerstonejs/tools';

import {
    KONVA_TENSION,
    KonvaTensionSpline,
    TOOTH_TENSION,
    cubicPoint,
    konvaTensionCurve,
    tensionControls,
    toothSplineConfiguration,
} from '../imaging/annotations/tensionSpline.js';

/** Deterministic pseudo-random, so a failure is reproducible. */
function ring(count, radiusX, radiusY, angleJitter, radiusJitter, seed) {
    let state = seed;
    const random = () => {
        state = (state * 1103515245 + 12345) & 0x7fffffff;
        return state / 0x7fffffff;
    };
    return Array.from({ length: count }, (_unused, index) => {
        const angle =
            (index / count) * 2 * Math.PI +
            (random() - 0.5) * angleJitter * ((2 * Math.PI) / count);
        const radius = 1 + (random() - 0.5) * radiusJitter;
        return [
            400 + radiusX * radius * Math.cos(angle),
            400 + radiusY * radius * Math.sin(angle),
        ];
    });
}

/** Worst distance from any point of `curve` to the nearest point of `reference`. */
function maxDeviation(curve, reference) {
    let worst = 0;
    for (const point of curve) {
        let best = Infinity;
        for (const other of reference) {
            best = Math.min(best, Math.hypot(point[0] - other[0], point[1] - other[1]));
        }
        worst = Math.max(worst, best);
    }
    return worst;
}

function polyline(polygon, { resolution = 39 } = {}) {
    const spline = new KonvaTensionSpline({ scale: TOOTH_TENSION, resolution });
    spline.closed = true;
    spline.setControlPoints(polygon.map((point) => [point[0], point[1]]));
    return spline.getPolylinePoints().map((point) => [point[0], point[1]]);
}

/**
 * The cases that matter, and why each one is here.
 *
 * `even` is the case where a uniform cardinal spline would also have been fine, so it
 * proves nothing on its own. The uneven ones are where Konva's chord-length weighting
 * diverges from a uniform spline, and `sparse` is the worst of them -- a premolar drawn
 * with eight vertices, where the measured error of Cornerstone's own `CardinalSpline` is
 * about 3.5 px.
 */
const CASES = [
    ['12 vertices, evenly spaced', ring(12, 90, 120, 0, 0, 7)],
    ['12 vertices, unevenly spaced', ring(12, 90, 120, 0.8, 0.1, 11)],
    ['8 vertices, very unevenly spaced', ring(8, 70, 110, 1.2, 0.18, 31)],
    ['30 vertices, dense', ring(30, 95, 125, 0.95, 0.08, 41)],
];

describe('KonvaTensionSpline', () => {
    for (const [name, polygon] of CASES) {
        it(`draws the Konva tension curve: ${name}`, () => {
            const deviation = maxDeviation(konvaTensionCurve(polygon), polyline(polygon));
            assert.ok(
                deviation < 1e-9,
                `${name}: deviates by ${deviation} px from the curve the old editor drew`
            );
        });
    }

    it('is measurably closer than the uniform spline it replaces', () => {
        // The claim the module's table makes, asserted rather than left as a comment: if a
        // bump ever made upstream's own CardinalSpline chord-weighted too, this fails and
        // the override can be deleted.
        const polygon = ring(8, 70, 110, 1.2, 0.18, 31);
        const uniform = new splines.CardinalSpline({ scale: 0.5, resolution: 39 });
        uniform.closed = true;
        uniform.setControlPoints(polygon.map((point) => [point[0], point[1]]));

        const reference = konvaTensionCurve(polygon);
        const uniformDeviation = maxDeviation(
            reference,
            uniform.getPolylinePoints().map((point) => [point[0], point[1]])
        );
        assert.ok(
            uniformDeviation > 1,
            `the uniform spline is only ${uniformDeviation} px off; the override may be ` +
                'unnecessary now'
        );
    });

    it('keeps the stored polygon as its control points', () => {
        // The whole reason this approach is safe: rendering changed, storage did not.
        const polygon = ring(12, 90, 120, 0.8, 0.1, 11);
        const spline = new KonvaTensionSpline({ scale: TOOTH_TENSION });
        spline.closed = true;
        spline.setControlPoints(polygon.map((point) => [point[0], point[1]]));
        assert.deepEqual(spline.getControlPoints(), polygon);
    });

    it('draws an open contour without wrapping around', () => {
        // `allowOpenSplines` is off for teeth, but a half-finished ring is open while it is
        // being drawn, and upstream's mirroring for the end segments has to keep working.
        const polygon = [
            [0, 0],
            [10, 20],
            [30, 25],
            [50, 5],
        ];
        const spline = new KonvaTensionSpline({ scale: TOOTH_TENSION, resolution: 9 });
        spline.closed = false;
        spline.setControlPoints(polygon);
        const points = spline.getPolylinePoints();
        assert.ok(points.length > polygon.length);
        for (const [x, y] of points) {
            assert.ok(Number.isFinite(x) && Number.isFinite(y), 'no NaN from the mirrored ends');
        }
    });

    it('degrades to a straight segment on coincident vertices instead of NaN', () => {
        // A double-click can land two vertices on the same pixel. Konva's weights divide by
        // the summed chord length, which is then zero.
        const controls = tensionControls([5, 5], [5, 5], [5, 5], TOOTH_TENSION);
        assert.deepEqual(controls, { before: [5, 5], after: [5, 5] });

        const spline = new KonvaTensionSpline({ scale: TOOTH_TENSION, resolution: 5 });
        spline.closed = true;
        spline.setControlPoints([
            [0, 0],
            [10, 0],
            [10, 0],
            [0, 10],
        ]);
        for (const [x, y] of spline.getPolylinePoints()) {
            assert.ok(Number.isFinite(x) && Number.isFinite(y));
        }
    });

    it('reads its tension from the configured scale, not a baked constant', () => {
        // Which is why the class extends CardinalSpline: `_updateSplineInstance` only
        // pushes `scale` onto instances that pass `instanceof CardinalSpline`.
        const spline = new KonvaTensionSpline();
        assert.ok(spline instanceof splines.CardinalSpline);
        assert.equal(spline.scale, TOOTH_TENSION);

        const polygon = ring(12, 90, 120, 0.8, 0.1, 11);
        spline.closed = true;
        spline.setControlPoints(polygon.map((point) => [point[0], point[1]]));
        spline.scale = 0;
        const straight = spline.getPolylinePoints().map((point) => [point[0], point[1]]);
        // Tension 0 is the polygon itself, so the vertices must lie on the drawn line.
        assert.ok(maxDeviation(polygon, straight) < 1e-9);
    });
});

describe('the upstream hooks this override stands on', () => {
    const { CardinalSpline, CubicSpline } = splines;

    it('still has the private methods the override calls', () => {
        for (const name of ['_getPoint', '_getCurveSegmentPoints', '_getNumCurveSegments']) {
            assert.equal(
                typeof CubicSpline.prototype[name],
                'function',
                `CubicSpline.${name} is gone; KonvaTensionSpline no longer overrides what ` +
                    'it thinks it does and every tooth will render as a uniform spline'
            );
        }
    });

    it('still routes point generation through _getPoint', () => {
        // The load-bearing assumption: overriding `_getPoint` is enough to change the drawn
        // curve. If upstream ever inlines the matrix evaluation into `_getLineSegments`,
        // parity above would still pass while the *tool* rendered something else -- so this
        // asserts the call actually reaches the override.
        let calls = 0;
        class Counting extends KonvaTensionSpline {
            _getPoint(...args) {
                calls += 1;
                return super._getPoint(...args);
            }
        }
        const spline = new Counting({ scale: TOOTH_TENSION, resolution: 9 });
        spline.closed = true;
        spline.setControlPoints([
            [0, 0],
            [10, 0],
            [10, 10],
            [0, 10],
        ]);
        spline.getPolylinePoints();
        assert.ok(calls > 0, 'getPolylinePoints() no longer goes through _getPoint');
    });

    it('still exposes scale as a settable property on CardinalSpline', () => {
        // The reason for the base class, asserted so it does not quietly stop being true:
        // `SplineROITool._updateSplineInstance` assigns `spline.scale` from the tool config,
        // and only for instances that pass `instanceof CardinalSpline`.
        const descriptor = Object.getOwnPropertyDescriptor(CardinalSpline.prototype, 'scale');
        assert.ok(descriptor, 'CardinalSpline.scale is gone');
        assert.equal(typeof descriptor.set, 'function', 'CardinalSpline.scale is read-only');
    });
});

describe('toothSplineConfiguration', () => {
    it('registers the class under its own type key', () => {
        const configuration = toothSplineConfiguration();
        assert.equal(configuration.type, KONVA_TENSION);
        assert.equal(configuration.configuration[KONVA_TENSION].Class, KonvaTensionSpline);
        assert.equal(configuration.configuration[KONVA_TENSION].scale, TOOTH_TENSION);
    });

    it('leaves vertex add and delete on', () => {
        // The midpoint handles the old editor hand-rolled. `curveMidpoint` existed only to
        // place them on the drawn curve; upstream inserts at the closest point's `u`, which
        // is on the curve by construction.
        const { [KONVA_TENSION]: config } = toothSplineConfiguration().configuration;
        assert.equal(config.controlPointAdditionEnabled, true);
        assert.equal(config.controlPointDeletionEnabled, true);
    });
});

describe('cubicPoint', () => {
    it('hits the endpoints exactly', () => {
        const start = [0, 0];
        const end = [10, 10];
        assert.deepEqual(cubicPoint(start, [1, 0], [9, 10], end, 0), start);
        assert.deepEqual(cubicPoint(start, [1, 0], [9, 10], end, 1), end);
    });
});
