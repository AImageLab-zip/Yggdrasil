import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { splines } from '@cornerstonejs/tools';

import {
    ARCH_SPLINE,
    ArchSpline,
    archSplineConfiguration,
    centripetalPoint,
    knotStep,
} from '../imaging/panoramic/archSpline.js';

/**
 * The baker's own implementation, loaded from the file Phase 7 leaves untouched.
 *
 * Not a transcription: `static/js/seg2pano_core.js` is what produces the arch the strips
 * are baked from, so parity has to be asserted against *it*, or the two can drift and the
 * drawn curve will quietly stop being the curve the projection follows.
 */
const core = createRequire(import.meta.url)('../../static/js/seg2pano_core.js');

/** Control points with deliberately uneven spacing -- where uniform and centripetal differ. */
const ARCH = [
    [10, 60], [22, 44], [39, 33], [58, 30], [78, 34], [95, 47], [104, 66],
];

test('the upstream hooks the subclass stands on still exist', () => {
    // Same guard as `tensionSpline.test.js` and `attenuatedMip.test.js`: the override is
    // on private names, so a Cornerstone bump that renames one must fail the build rather
    // than silently drawing a uniform Catmull-Rom a few pixels off across the whole arch.
    assert.equal(typeof splines.CubicSpline, 'function');
    const prototype = splines.CubicSpline.prototype;
    assert.equal(typeof prototype._getPoint, 'function');
    assert.equal(typeof prototype._getNumCurveSegments, 'function');
    assert.equal(typeof prototype._getCurveSegmentPoints, 'function');
    assert.ok(ArchSpline.prototype instanceof splines.CubicSpline);

    // **And the base must be `CardinalSpline`, not `CubicSpline`.** `getTransformMatrix`
    // is abstract on `Spline`; `CubicSpline` calls it and does not define it, so a class
    // that stops short of `CardinalSpline` throws on the first render -- which is what
    // took the control points off the screen. Asserted as a required method, because the
    // previous form of this suite reached for it as `getTransformMatrix?.()` and that
    // optional call is precisely what hid the defect.
    assert.ok(ArchSpline.prototype instanceof splines.CardinalSpline);
    assert.equal(typeof ArchSpline.prototype.getTransformMatrix, 'function');
    assert.ok(Array.isArray(new ArchSpline().getTransformMatrix()));
});

test('the arch renders through the entry point the tool actually calls', () => {
    // `SplineROITool` never calls `_getPoint`. It calls `getPolylinePoints()`, which goes
    // through `Spline._update` -> `CubicSpline.getSplineCurves` -> `getTransformMatrix`.
    // Every other case here reaches past that path, which is how a missing base-class
    // method survived a full suite.
    const spline = new ArchSpline();
    spline.setControlPoints(ARCH);
    spline.closed = false;

    const polyline = spline.getPolylinePoints();
    assert.ok(polyline.length > ARCH.length, 'the curve was resampled, not echoed back');
    assert.ok(polyline.every(([x, y]) => Number.isFinite(x) && Number.isFinite(y)));
    assert.ok(spline.length > 0, 'arc length was computed from real curve segments');

    // Two points is the shortest thing a fitted arch can be handed; the segment guard in
    // `getSplineCurves` only spares an empty spline, so this is the smallest input that
    // still reaches the matrix.
    const pair = new ArchSpline();
    pair.setControlPoints(ARCH.slice(0, 2));
    assert.ok(pair.getPolylinePoints().length >= 2);
});

test('the knot spacing is centripetal, not uniform', () => {
    // alpha = 0.5, i.e. the fourth root of the squared distance.
    assert.equal(knotStep([0, 0], [3, 4]), 5 ** 0.5);
    assert.equal(knotStep([1, 1], [1, 1]), 0);
});

test('a segment matches the baker chain point for point', () => {
    const [p0, p1, p2, p3] = ARCH.slice(0, 4);
    const chain = core.catmullRomChain([p0, p1, p2, p3]);
    assert.ok(chain.length > 4, 'the reference produced a curve to compare against');

    // `catmullRomSegment` samples the knot interval [t1, t2] uniformly; the subclass walks
    // u in [0, 1] across the same interval. Re-deriving the u values here is what makes
    // this an exact comparison rather than a nearest-point tolerance.
    const t1 = knotStep(p0, p1);
    const t2 = t1 + knotStep(p1, p2);
    const count = chain.length;
    chain.forEach((expected, index) => {
        const t = t1 + ((t2 - t1) * index) / (count - 1);
        const actual = centripetalPoint(p0, p1, p2, p3, (t - t1) / (t2 - t1));
        assert.ok(Math.abs(actual[0] - expected[0]) < 1e-9, `x at ${index}`);
        assert.ok(Math.abs(actual[1] - expected[1]) < 1e-9, `y at ${index}`);
    });
});

test('the curve passes exactly through the control points it interpolates', () => {
    const [p0, p1, p2, p3] = ARCH.slice(0, 4);

    const start = centripetalPoint(p0, p1, p2, p3, 0);
    const end = centripetalPoint(p0, p1, p2, p3, 1);

    // Interpolating, not approximating: a dragged handle sits on the arch it draws, which
    // is the whole reason the reader can trust where they put it.
    assert.ok(Math.hypot(start[0] - p1[0], start[1] - p1[1]) < 1e-9);
    assert.ok(Math.hypot(end[0] - p2[0], end[1] - p2[1]) < 1e-9);
});

test('coincident control points degrade to a straight segment, not to NaN', () => {
    assert.equal(centripetalPoint([5, 5], [5, 5], [9, 9], [12, 12], 0.5), null);

    const spline = new ArchSpline();
    spline.setControlPoints([[5, 5], [5, 5], [9, 9], [12, 12]]);
    const point = spline._getPoint(1.5, spline.getTransformMatrix());

    assert.ok(Number.isFinite(point[0]) && Number.isFinite(point[1]));
});

test('the drawn arch agrees with the baker over every interior segment', () => {
    const spline = new ArchSpline();
    spline.setControlPoints(ARCH);
    spline.closed = false;
    const matrix = spline.getTransformMatrix();

    // Cornerstone's open segment `j` spans control points j..j+1 from the quadruple
    // (j-1, j, j+1, j+2). The chain's segment `i` spans the quadruple (i, i+1, i+2, i+3),
    // so the two line up at i = j - 1 for every j the chain covers.
    for (let j = 1; j <= ARCH.length - 3; j += 1) {
        const quad = ARCH.slice(j - 1, j + 3);
        for (const u of [0, 0.25, 0.5, 0.75, 1]) {
            const expected = centripetalPoint(...quad, u);
            const actual = spline._getPoint(j + u, matrix);
            const at = `segment ${j} at u=${u}`;
            assert.ok(Math.abs(actual[0] - expected[0]) < 1e-9, `x, ${at}`);
            assert.ok(Math.abs(actual[1] - expected[1]) < 1e-9, `y, ${at}`);
        }
    }
});

test('an open arch is not silently extended past its last control point', () => {
    const spline = new ArchSpline();
    spline.setControlPoints(ARCH);
    spline.closed = false;

    assert.equal(spline._getPoint(ARCH.length, []), undefined);
    assert.equal(spline._getPoint(-1, []), undefined);
});

test('the tool configuration registers the class under its own type key', () => {
    const configuration = archSplineConfiguration();

    assert.equal(configuration.type, ARCH_SPLINE);
    assert.equal(configuration.configuration[ARCH_SPLINE].Class, ArchSpline);
    assert.equal(configuration.configuration[ARCH_SPLINE].controlPointAdditionEnabled, true);
    assert.equal(configuration.configuration[ARCH_SPLINE].controlPointDeletionEnabled, true);

    // Only keys of `DEFAULT_SPLINE_CONFIG` are read, so the invented `allowOpen` /
    // `allowClosed` / `allowOpenEdit` triple that used to sit here said nothing. The arch
    // running condyle to condyle is stated where it is honoured: `allowOpenSplines` on
    // the tool, asserted in `archSurface.test.js`.
    for (const invented of ['allowOpen', 'allowClosed', 'allowOpenEdit']) {
        assert.ok(
            !(invented in configuration.configuration[ARCH_SPLINE]),
            `${invented} is not a spline config key and must not read as one`
        );
    }
});
