import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { markersFor, markerUid, landmarkForUid } from '../imaging/mesh/landmarkMarkers.js';
import { emptyDocument, place } from '../imaging/mesh/landmarkDocument.js';
import { displayCoordinates, isPlacementEvent, isSelectionEvent } from '../imaging/mesh/pickMath.js';
import { cameraFor, distanceForBounds } from '../imaging/mesh/cameraPresets.js';

/**
 * The coordinate-parity suite: the one thing every stored landmark depends on.
 *
 * A landmark is `resource_local` -- the STL's own object space. The legacy viewer rotated
 * each jaw 180 degrees about Y and translated both by the negated centre of their combined
 * bounding box, then stored `mesh.worldToLocal(hit.point)`. Because `worldToLocal` inverts
 * the *full* world matrix, both transforms cancel and the stored numbers are raw STL
 * vertex coordinates.
 *
 * Cornerstone's `Mesh` applies no transform to an STL actor, so untransformed actors make
 * a picked world position identical to the value to store. These tests assert that
 * identity as a property -- the absence of any conversion -- rather than trusting a
 * comment, and then assert the upstream fact it rests on.
 */

test('a picked world position is stored unchanged', () => {
    const picked = [1.5, -2.25, 3.125];
    const document = emptyDocument();
    place(document, { jaw: 'upper', tooth: '11', type: 'incisal', point: picked });
    // Bit-identical, not "close": a conversion that rounded would be invisible here and
    // visible on a study, as landmarks that drift a little further with every save.
    assert.deepEqual(document.upper['11'].incisal, picked);
    assert.equal(
        Object.is(document.upper['11'].incisal[2], 3.125),
        true,
        'the coordinate survived exactly',
    );
});

test('a marker is drawn at the stored coordinates, with no inverse applied', () => {
    const stored = [-10.5, 11.25, -12.0625];
    const document = emptyDocument();
    place(document, { jaw: 'lower', tooth: '31', type: 'gingival', point: stored });
    const [marker] = markersFor(document, {});
    assert.deepEqual(marker.position, stored);
});

test('pick to store to marker is the identity, round trip', () => {
    const picked = [3.25, -4.5, 5.75];
    const document = emptyDocument();
    place(document, { jaw: 'upper', tooth: '26', type: 'outer', point: picked });
    const [marker] = markersFor(document, {});
    assert.deepEqual(marker.position, picked);
    assert.deepEqual(landmarkForUid(document, marker.uid).point, picked);
});

/**
 * The upstream assumption, pinned the way `tensionSpline.test.js` pins its private hooks.
 *
 * If a future `@cornerstonejs/core` starts centring or reorienting mesh actors, the
 * identity above silently stops holding and every landmark on every historical study moves
 * -- while still looking like a perfectly plausible scan. Reading the shipped file means
 * that becomes a failing build instead.
 */
test('the shipped Mesh class still applies no transform to STL actors', () => {
    const source = readFileSync(
        new URL('../../node_modules/@cornerstonejs/core/dist/esm/cache/classes/Mesh.js', import.meta.url),
        'utf8',
    );
    for (const transform of [
        'setUserMatrix',
        'setOrigin',
        'setPosition',
        'setScale',
        'setOrientation',
        'rotateX',
        'rotateY',
        'rotateZ',
    ]) {
        assert.ok(
            !source.includes(transform),
            `Mesh.js now calls ${transform}: mesh actors are no longer in the STL's own ` +
            'frame, so every stored landmark is offset. See frontend/imaging/mesh/' +
            'landmarkMarkers.js for what this test protects.',
        );
    }
    // And the class this rests on is still the one being read.
    assert.ok(source.includes('export class Mesh'));
    assert.ok(source.includes('vtkSTLReader'));
});

test('marker uids survive a deletion earlier in the order', () => {
    // Derived from the landmark's identity rather than a counter, so removing one marker
    // does not renumber the rest and strand the selection on a different point.
    const document = emptyDocument();
    place(document, { jaw: 'upper', tooth: '11', type: 'cusps', point: [1, 1, 1] });
    place(document, { jaw: 'upper', tooth: '11', type: 'cusps', point: [2, 2, 2] });
    assert.equal(markerUid({ jaw: 'upper', tooth: '11', type: 'cusps', index: 1 }), 'upper:11:cusps:1');
});

// ---------------------------------------------------------------------------
// The offscreen pick transform
// ---------------------------------------------------------------------------

test('a full-canvas viewport only flips y', () => {
    assert.deepEqual(
        displayCoordinates({
            offsetX: 100, offsetY: 50,
            canvasWidth: 800, canvasHeight: 600,
            viewport: [0, 0, 1, 1],
        }),
        [100, 550, 0],
    );
});

test('devicePixelRatio scales the pointer position', () => {
    const [x] = displayCoordinates({
        offsetX: 100, offsetY: 50,
        canvasWidth: 1600, canvasHeight: 1200,
        viewport: [0, 0, 1, 1],
        devicePixelRatio: 2,
    });
    assert.equal(x, 200);
});

test('a partial viewport rect scales both axes', () => {
    // The case that actually occurs: the patient-detail page mounts the volume grid and
    // the photo stack into the same shared offscreen canvas, so this surface owns a
    // sub-rectangle of it. Assuming [0,0,1,1] puts every pick a few millimetres out --
    // which reads as a coordinate bug in the landmark model and is not one.
    const [x, y] = displayCoordinates({
        offsetX: 100, offsetY: 50,
        canvasWidth: 800, canvasHeight: 600,
        viewport: [0, 0, 0.5, 0.5],
    });
    assert.equal(x, 50);
    assert.equal(y, 300 - 25);
});

test('shift plus primary places; primary alone selects', () => {
    assert.ok(isPlacementEvent({ button: 0, shiftKey: true }));
    assert.ok(!isPlacementEvent({ button: 0, shiftKey: false }));
    assert.ok(!isPlacementEvent({ button: 2, shiftKey: true }));
    assert.ok(isSelectionEvent({ button: 0, shiftKey: false }));
    assert.ok(!isSelectionEvent({ button: 0, shiftKey: true }));
});

// ---------------------------------------------------------------------------
// Cameras
// ---------------------------------------------------------------------------

test('all seven presets resolve, and the odd viewUps are kept verbatim', () => {
    // `viewUpper` and `viewLower` are at 45 degrees and are not unit vectors. That is what
    // the legacy viewer did and what clinicians have learned; vtk normalises viewUp itself,
    // so "fixing" them would rotate two views for no reason anybody asked for.
    assert.deepEqual(cameraFor('upper', 10).viewUp, [0, 1, -1]);
    assert.deepEqual(cameraFor('lower', 10).viewUp, [0, -1, 1]);
    for (const name of ['reset', 'front', 'right', 'left']) {
        assert.deepEqual(cameraFor(name, 10).viewUp, [0, 0, -1]);
    }
    assert.equal(cameraFor('nope'), null);
});

test('presets point from the focal point outward at the given distance', () => {
    assert.deepEqual(cameraFor('reset', 80).position, [0, 80, 0]);
    assert.deepEqual(cameraFor('right', 5).position, [-5, 0, 0]);
    assert.deepEqual(cameraFor('left', 5).position, [5, 0, 0]);
    assert.deepEqual(cameraFor('reset').focalPoint, [0, 0, 0]);
});

test('the framing distance uses the diagonal, and falls back on nonsense bounds', () => {
    // Half the diagonal rather than half the largest side, so a wide shallow arch is not
    // cropped when the camera swings to the side.
    assert.ok(distanceForBounds([-10, 10, -5, 5, -2, 2]) > 20);
    assert.equal(distanceForBounds(null), 80);
    assert.equal(distanceForBounds([0, 0, 0, 0, 0, 0]), 80);
    assert.equal(distanceForBounds([1, 2, 3]), 80);
});

test('the shipped STL reader still writes the cell scalars we switch off', () => {
    /**
     * Why `setScalarVisibility(false)` is in `meshViewport.js`.
     *
     * `vtkSTLReader` sets cell scalars named "Attribute" from the binary STL's per-facet
     * attribute-byte field -- a padding word almost every exporter writes as zero -- and
     * `vtkMapper` defaults to colouring by scalars. So the arches rendered scarlet through
     * the default lookup table while the property colour `Mesh` had set was ignored.
     *
     * Read from the shipped file so the line cannot be removed as pointless: if a future
     * vtk.js stops writing those scalars, this fails and says why the line existed.
     */
    const source = readFileSync(
        new URL('../../node_modules/@kitware/vtk.js/IO/Geometry/STLReader.js', import.meta.url),
        'utf8',
    );
    assert.ok(
        source.includes('getCellData().setScalars'),
        'STLReader no longer sets cell scalars: the setScalarVisibility(false) in ' +
        'meshViewport.js may now be unnecessary -- check before deleting it.',
    );
    assert.ok(source.includes('"Attribute"'));

    const mapper = readFileSync(
        new URL('../../node_modules/@kitware/vtk.js/Rendering/Core/Mapper.js', import.meta.url),
        'utf8',
    );
    assert.ok(
        /scalarVisibility:\s*true/.test(mapper),
        'vtkMapper no longer defaults scalarVisibility to true',
    );
});
