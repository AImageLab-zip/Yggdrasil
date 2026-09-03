/**
 * Cornerstone contours <-> the teeth map.
 *
 * The property that matters most here is **ordering**, and it matters for a reason that is
 * not aesthetic: `annotations/adapters/tooth_segmentation.py` orders by FDI code then
 * polygon index so two conversions of one study are byte-identical, and
 * `annotations_crosscheck` compares the converted legacy rows against the live ones field
 * by field. A client that emitted a different order would make the cross-check report
 * drift on every study anybody had edited, burying the signal it exists to give.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
    MIN_VERTICES,
    centroidOf,
    clampToImage,
    fdiOf,
    normalizeTeeth,
    outlinesToDraw,
    roundCoordinate,
    setFdi,
    teethDiffer,
    teethFromAnnotations,
    unassignedOutlines,
} from '../imaging/photos/toothOutlines.js';

const TOOL = 'ToothOutline';
const IMAGE = 'yggweb:/maxillo/api/.../11/a.jpg';
const OTHER_IMAGE = 'yggweb:/maxillo/api/.../12/b.jpg';

/** Identity converters: this module's job is the shape, not the arithmetic. */
const worldToImage = (_imageId, world) => [world[0], world[1]];
const imageToWorld = (_imageId, point) => [point[0], point[1], 0];

function outline({ label, points, imageId = IMAGE, toolName = TOOL }) {
    return {
        annotationUID: `uid-${label}-${points.length}-${Math.trunc(points[0][0])}`,
        data: { label, handles: { points: points.map(([x, y]) => [x, y, 0]) } },
        metadata: { toolName, referencedImageId: imageId },
    };
}

const RING_A = [
    [10, 10],
    [30, 10],
    [30, 30],
    [10, 30],
];
const RING_B = [
    [40, 40],
    [60, 40],
    [60, 60],
];

describe('fdiOf and setFdi', () => {
    it('reads the annotation label, which is where getTextLines can see it', () => {
        assert.equal(fdiOf(outline({ label: '36', points: RING_A })), '36');
    });

    it('is null for an unassigned or nonsensical label', () => {
        assert.equal(fdiOf(outline({ label: '', points: RING_A })), null);
        assert.equal(fdiOf(outline({ label: '99', points: RING_A })), null);
        assert.equal(fdiOf({}), null);
    });

    it('setFdi works on an annotation with no data yet', () => {
        const annotation = {};
        setFdi(annotation, '11');
        assert.equal(fdiOf(annotation), '11');
    });
});

describe('teethFromAnnotations', () => {
    it('orders by FDI code, matching the server adapter', () => {
        const teeth = teethFromAnnotations(
            [
                outline({ label: '36', points: RING_B }),
                outline({ label: '11', points: RING_A }),
            ],
            { imageId: IMAGE, worldToImage, toolName: TOOL }
        );
        assert.deepEqual(Object.keys(teeth), ['11', '36']);
    });

    it('keeps encounter order within one tooth, which is the stored polygon index', () => {
        const teeth = teethFromAnnotations(
            [
                outline({ label: '36', points: RING_A }),
                outline({ label: '36', points: RING_B }),
            ],
            { imageId: IMAGE, worldToImage, toolName: TOOL }
        );
        assert.deepEqual(teeth['36'], [RING_A, RING_B]);
    });

    it('reads only this image, and only this tool', () => {
        const teeth = teethFromAnnotations(
            [
                outline({ label: '36', points: RING_A }),
                outline({ label: '11', points: RING_A, imageId: OTHER_IMAGE }),
                outline({ label: '21', points: RING_A, toolName: 'Length' }),
            ],
            { imageId: IMAGE, worldToImage, toolName: TOOL }
        );
        assert.deepEqual(Object.keys(teeth), ['36']);
    });

    it('leaves out an unassigned contour rather than inventing a tooth for it', () => {
        // Inventing one would export a polygon under a tooth nobody chose. The state
        // machine reports these separately -- see unassignedOutlines.
        const annotations = [
            outline({ label: '', points: RING_A }),
            outline({ label: '36', points: RING_B }),
        ];
        const teeth = teethFromAnnotations(annotations, {
            imageId: IMAGE,
            worldToImage,
            toolName: TOOL,
        });
        assert.deepEqual(Object.keys(teeth), ['36']);
        assert.equal(unassignedOutlines(annotations, { imageId: IMAGE, toolName: TOOL }).length, 1);
    });

    it('drops a ring with fewer than three vertices', () => {
        const teeth = teethFromAnnotations(
            [outline({ label: '36', points: RING_A.slice(0, MIN_VERTICES - 1) })],
            { imageId: IMAGE, worldToImage, toolName: TOOL }
        );
        assert.deepEqual(teeth, {});
    });

    it('clamps a vertex dragged off the image, and rounds to three places', () => {
        // A vertex outside the bytes it was drawn on cannot be re-projected when the photo
        // is cropped -- the replay would carry it somewhere meaningless.
        const teeth = teethFromAnnotations(
            [
                outline({
                    label: '36',
                    points: [
                        [-5, 10.00049],
                        [900, 10],
                        [30, 700],
                    ],
                }),
            ],
            { imageId: IMAGE, worldToImage, toolName: TOOL, bounds: { width: 800, height: 600 } }
        );
        assert.deepEqual(teeth["36"], [
            [
                [0, 10],
                [800, 10],
                [30, 600],
            ],
        ]);
    });
});

describe('outlinesToDraw', () => {
    it('is ordered by code then polygon index, so a restore draws them as stored', () => {
        const outlines = outlinesToDraw(
            { 36: [RING_A, RING_B], 11: [RING_A] },
            { imageId: IMAGE, imageToWorld }
        );
        assert.deepEqual(
            outlines.map((entry) => [entry.fdi, entry.polygonIndex]),
            [
                ['11', 0],
                ['36', 0],
                ['36', 1],
            ]
        );
    });

    it('skips a degenerate ring from the legacy corpus', () => {
        // The adapter refuses these, so drawing one would hand the user a shape they
        // cannot save.
        const outlines = outlinesToDraw({ 36: [[[1, 1], [2, 2]]] }, { imageId: IMAGE, imageToWorld });
        assert.deepEqual(outlines, []);
    });

    it('round-trips through teethFromAnnotations', () => {
        const teeth = { 11: [RING_A], 36: [RING_B, RING_A] };
        const drawn = outlinesToDraw(teeth, { imageId: IMAGE, imageToWorld }).map((entry) =>
            outline({
                label: entry.fdi,
                points: entry.points.map(([x, y]) => [x, y]),
            })
        );
        assert.deepEqual(
            teethFromAnnotations(drawn, { imageId: IMAGE, worldToImage, toolName: TOOL }),
            normalizeTeeth(teeth)
        );
    });
});

describe('teethDiffer', () => {
    it('ignores the float noise a JSON round trip introduces', () => {
        // Compared on the rounded map rather than on annotation objects: Cornerstone
        // rewrites cachedStats and handle sub-objects on every render, so an object
        // comparison would report a change forever and the autosave would never stop.
        assert.equal(teethDiffer({ 11: [RING_A] }, { 11: [RING_A.map(([x, y]) => [x + 1e-9, y])] }), false);
    });

    it('is insensitive to key order and sees a real move', () => {
        assert.equal(teethDiffer({ 11: [RING_A], 36: [RING_B] }, { 36: [RING_B], 11: [RING_A] }), false);
        assert.equal(teethDiffer({ 11: [RING_A] }, { 11: [RING_B] }), true);
    });

    it('an emptied tooth differs from a present one', () => {
        assert.equal(teethDiffer({ 11: [RING_A] }, {}), true);
        assert.equal(teethDiffer({ 11: [] }, {}), false, 'an empty list is no tooth');
    });
});

describe('roundCoordinate and clampToImage', () => {
    it("rounds to three places, matching the old editor and the server", () => {
        // `Number(value.toFixed(3))`, deliberately -- the same call the Konva editor made,
        // down to its binary-rounding quirk at the halfway point. Choosing a "better"
        // rounding here would make every stored coordinate differ from what the old
        // editor would have written for the same drag, on every study.
        assert.equal(roundCoordinate(1.00049), 1);
        assert.equal(roundCoordinate(1.0006), 1.001);
        assert.equal(roundCoordinate(1.0005), 1, "toFixed rounds this one down");
    });

    it('leaves a point alone when the bounds are unknown', () => {
        assert.deepEqual(clampToImage([-5, 900], null), [-5, 900]);
        assert.deepEqual(clampToImage([-5, 900], { width: 0, height: 0 }), [-5, 900]);
    });
});

describe('centroidOf', () => {
    it('is where the FDI code goes: the middle of the ring', () => {
        // A square, so the answer is not in doubt.
        assert.deepEqual(centroidOf([[0, 0], [10, 0], [10, 10], [0, 10]]), [5, 5]);
    });

    it('ignores anything past the first two components', () => {
        // The caller hands over canvas points, but a world point carries a z it must not
        // be tripped by.
        assert.deepEqual(centroidOf([[0, 0, 7], [4, 2, -3]]), [2, 1]);
    });

    it('has no answer for an empty ring, and says so', () => {
        // Null rather than [0, 0]: the origin is a real place on the image, and a label
        // silently parked in the top-left corner is worse than no label.
        assert.equal(centroidOf([]), null);
        assert.equal(centroidOf(null), null);
        assert.equal(centroidOf(undefined), null);
    });
});
