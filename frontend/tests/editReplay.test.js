import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
    applyOperation,
    clipPolygonToRect,
    transformPolygon,
    transformTeeth,
} from '../imaging/photos/editReplay.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..', '..');

// The same file `maxillo/tests_image_edit_replay.py` reads. Two implementations of one
// algorithm existed independently and drifted -- the server was missing both rotate
// cases, so a rotated photograph read back untransformed polygons and the segmentation
// silently detached from the anatomy. Driving both from one fixture is what stops that
// happening again: a change on either side that is not matched fails on both.
const FIXTURE = JSON.parse(
    readFileSync(join(REPO, 'common', 'fixtures', 'image_edit_replay.json'), 'utf8')
);

test('the shared fixture is present and non-empty', () => {
    // A vanished fixture must fail loudly rather than pass zero cases in silence -- the
    // exact way a shared contract stops being one without anybody noticing.
    assert.ok(FIXTURE.cases.length >= 10, `only ${FIXTURE.cases.length} cases`);
    const names = FIXTURE.cases.map((entry) => entry.name);
    assert.equal(new Set(names).size, names.length, 'case names must be unique');
});

test('every shared case matches', () => {
    for (const entry of FIXTURE.cases) {
        assert.deepEqual(
            transformPolygon(entry.polygon, entry.operations),
            entry.expected,
            entry.name
        );
    }
});

test('the fixture actually exercises both rotate operations', () => {
    // Asserted separately from the loop because the failure being prevented is not a
    // wrong number: it is the identity transform quietly standing in for a rotation,
    // which every non-rotate case would still pass.
    const covered = new Set(
        FIXTURE.cases.flatMap((entry) => entry.operations.map((operation) => operation.type))
    );
    assert.ok(covered.has('rotate-cw'));
    assert.ok(covered.has('rotate-arbitrary'));
});

test('a rotation actually moves the polygon', () => {
    const polygon = [[10, 20], [30, 20], [30, 40]];
    const rotated = transformPolygon(polygon, [
        { type: 'rotate-cw', input_width: 100, input_height: 80 },
    ]);
    assert.notDeepEqual(rotated, polygon, 'the server used to return its input here');
});

test('an unknown operation is the identity, not an error', () => {
    // A viewer that threw on an operation it did not recognise would make the image
    // unopenable rather than merely unrotated.
    assert.deepEqual(applyOperation([10, 20], { type: 'posterize' }), [10, 20]);
    assert.deepEqual(applyOperation([10, 20], {}), [10, 20]);
});

test('clipping closes the ring along the cut rather than translating past it', () => {
    // A crop is not a coordinate shift for a polygon that crosses the new edge: the
    // part outside is gone. Translating without clipping leaves vertices at negative
    // coordinates, which render outside the image and read as a corrupt segmentation.
    const clipped = clipPolygonToRect(
        [[10, 10], [30, 10], [30, 30], [10, 30]],
        { left: 0, top: 0, right: 20, bottom: 100 }
    );
    assert.deepEqual(clipped, [[10, 10], [20, 10], [20, 30], [10, 30]]);
    assert.ok(clipped.every((point) => point[0] <= 20));
});

test('a polygon that clips to fewer than three points is dropped, not degenerate', () => {
    assert.deepEqual(
        clipPolygonToRect([[10, 10], [20, 10], [20, 20]], { left: 50, top: 50, right: 70, bottom: 70 }),
        []
    );
});

test('replay from the pristine geometry is idempotent', () => {
    // The property the whole preview path rests on: the editor replays from `baseTeeth`
    // on every keystroke rather than composing onto its own output. If this were not
    // idempotent the polygons would drift a little further with every preview.
    const teeth = { 11: [[[10, 20], [30, 20], [30, 40]]] };
    const edit = { operations: [{ type: 'flip-h', input_width: 100, input_height: 80 }] };
    assert.deepEqual(transformTeeth(teeth, edit), transformTeeth(teeth, edit));
});

test('composing a transform onto its own output is NOT the same as replaying', () => {
    // The mistake the idempotence above prevents, spelled out: a second flip returns to
    // where it started, so an accumulating preview would show no rotation at all.
    const teeth = { 11: [[[10, 20], [30, 20], [30, 40]]] };
    const edit = { operations: [{ type: 'flip-h', input_width: 100, input_height: 80 }] };
    const once = transformTeeth(teeth, edit);
    assert.notDeepEqual(transformTeeth(once, edit), once);
});

test('a tooth whose polygons all vanish is dropped from the map', () => {
    const out = transformTeeth(
        { 11: [[[10, 10], [20, 10], [20, 20], [10, 20]]] },
        { operations: [{ type: 'crop', x: 50, y: 50, width: 20, height: 20 }] }
    );
    assert.deepEqual(out, {}, 'a crop can remove a tooth from the picture entirely');
});

test('no operations returns the geometry unchanged, as numbers', () => {
    const teeth = { 11: [[['10', '20'], [30, 20], [30, 40]]] };
    assert.deepEqual(transformTeeth(teeth, { operations: [] }), {
        11: [[[10, 20], [30, 20], [30, 40]]],
    });
    assert.deepEqual(transformTeeth(teeth, null), { 11: [[[10, 20], [30, 20], [30, 40]]] });
});
