/**
 * The rasteriser that turns a freehand outline into a labelmap.
 *
 * The polygon button used to activate `PlanarFreehandContourSegmentationTool`, whose
 * `createAnnotation` throws unless a Contour segmentation is active -- and this surface
 * has only ever created labelmaps, so every stroke threw and was swallowed. The outline is
 * now drawn with a plain `PlanarFreehandROI` and burned in here, which keeps the labelmap
 * the only record. See `imaging/video/polygonFill.js` for the full argument.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { fillPolygon, polylineToPixels } from '../imaging/video/polygonFill.js';

/** The plane as rows of characters, so a test can state the shape it expects. */
function render(plane, width) {
    const rows = [];
    for (let y = 0; y * width < plane.length; y += 1) {
        rows.push(
            [...plane.slice(y * width, (y + 1) * width)].map((v) => (v ? '#' : '.')).join('')
        );
    }
    return rows;
}

test('a rectangle fills the pixels whose centres are inside it', () => {
    const plane = new Uint8Array(6 * 4);
    const touched = fillPolygon({
        plane,
        width: 6,
        height: 4,
        points: [[1, 1], [5, 1], [5, 3], [1, 3]],
    });

    assert.deepEqual(render(plane, 6), [
        '......',
        '.####.',
        '.####.',
        '......',
    ]);
    assert.equal(touched, 8);
});

test('a concave outline leaves its notch empty', () => {
    // Even-odd, not a bounding box: a reader circling around a vessel expects the vessel
    // left out, and a scanline fill that counted crossings wrongly would fill it in.
    const plane = new Uint8Array(7 * 5);
    fillPolygon({
        plane,
        width: 7,
        height: 5,
        points: [[1, 1], [6, 1], [6, 4], [4, 4], [4, 2], [3, 2], [3, 4], [1, 4]],
    });

    // The notch spans y in [2, 4], so both scanlines inside it -- 2.5 and 3.5 -- are cut.
    assert.deepEqual(render(plane, 7), [
        '.......',
        '.#####.',
        '.##.##.',
        '.##.##.',
        '.......',
    ]);
});

test('an outline is clipped to the frame rather than writing past it', () => {
    const plane = new Uint8Array(4 * 3);
    fillPolygon({
        plane,
        width: 4,
        height: 3,
        points: [[-20, -20], [40, -20], [40, 40], [-20, 40]],
    });

    assert.ok([...plane].every((value) => value === 1), 'every pixel, and no overflow');
});

test('a zero value erases, which is what the eraser draws with', () => {
    const plane = new Uint8Array(5 * 3).fill(1);
    const touched = fillPolygon({
        plane,
        width: 5,
        height: 3,
        points: [[1, 0], [4, 0], [4, 3], [1, 3]],
        value: 0,
    });

    assert.deepEqual(render(plane, 5), ['#...#', '#...#', '#...#']);
    assert.equal(touched, 9);
});

test('nothing to fill is answered with zero rather than a throw', () => {
    const plane = new Uint8Array(4 * 3);
    // Fewer than three points is a stray click, not a region.
    assert.equal(fillPolygon({ plane, width: 4, height: 3, points: [[0, 0], [1, 1]] }), 0);
    assert.equal(fillPolygon({ plane, width: 4, height: 3, points: [] }), 0);
    assert.equal(fillPolygon({ plane: null, width: 4, height: 3, points: [] }), 0);
    // A NaN would otherwise silently fill the whole plane through an infinite bound.
    assert.equal(
        fillPolygon({ plane, width: 4, height: 3, points: [[0, 0], [1, Number.NaN], [2, 2]] }),
        0
    );
    assert.ok([...plane].every((value) => value === 0));
});

test('a repeated fill counts only what it changed', () => {
    // The caller uses the count to tell a real stroke from a stray click.
    const plane = new Uint8Array(4 * 3);
    const square = [[0, 0], [4, 0], [4, 3], [0, 3]];
    assert.equal(fillPolygon({ plane, width: 4, height: 3, points: square }), 12);
    assert.equal(fillPolygon({ plane, width: 4, height: 3, points: square }), 0);
});

test('world points become pixels through the viewport that owns the mapping', () => {
    // `getImageData().imageData.worldToIndex` composes `worldToCanvas` with
    // `canvasToIndex`, so it follows the pan and zoom the reader drew under.
    const viewport = {
        getImageData: () => ({
            imageData: { worldToIndex: ([x, y]) => [x * 2, y * 2, 0] },
        }),
    };

    assert.deepEqual(polylineToPixels(viewport, [[1, 2, 0], [3, 4, 0]]), [[2, 4], [6, 8]]);
});

test('a viewport that cannot answer is reported rather than guessed at', () => {
    assert.equal(polylineToPixels({}, [[0, 0, 0]]), null);
    assert.equal(polylineToPixels({ getImageData: () => ({}) }, [[0, 0, 0]]), null);
    assert.equal(
        polylineToPixels(
            { getImageData: () => ({ imageData: { worldToIndex: () => [Number.NaN, 0, 0] } }) },
            [[0, 0, 0]]
        ),
        null,
        'a point that does not map is not a point at the origin'
    );
});
