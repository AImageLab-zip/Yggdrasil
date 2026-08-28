import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
    EDITABLE_TYPES,
    LANDMARK_TYPES,
    MULTI_POINT_TYPES,
    TEETH,
    cloneDocument,
    countForTooth,
    emptyDocument,
    fromState,
    jawForTooth,
    landmarks,
    place,
    refusePlacement,
    remove,
    sameLandmark,
    toSaveBody,
} from '../imaging/mesh/landmarkDocument.js';

/**
 * The same fixture `annotations/tests_ios_landmarks.py` reads.
 *
 * Shared on purpose: the document shape is implemented twice -- once in Python for the
 * record and the export, once here for the editor -- and a fixture both sides parse is
 * what stops the two drifting into a disagreement nobody notices until an export is wrong.
 */
const FIXTURE = JSON.parse(
    readFileSync(new URL('../../common/fixtures/ios_landmarks_document.json', import.meta.url)),
);

/** The fixture is stored in the legacy key format; the editor works per arch. */
function documentFromFixture() {
    const jaws = { upper: {}, lower: {} };
    for (const [key, entry] of Object.entries(FIXTURE.document)) {
        const [, jaw, , tooth] = key.split('_');
        jaws[jaw][tooth] = entry;
    }
    return fromState(jaws);
}

test('the vocabulary matches the server', () => {
    assert.equal(LANDMARK_TYPES.length, 10);
    assert.deepEqual([...MULTI_POINT_TYPES], ['cusps', 'planar']);
    // `planar` is the landmark job's output: rendered, never placed by hand.
    assert.ok(!EDITABLE_TYPES.includes('planar'));
    assert.equal(EDITABLE_TYPES.length, 9);
    assert.equal(TEETH.length, 32);
});

test('the jaw comes from the FDI quadrant, as the server derives it', () => {
    assert.equal(jawForTooth('11'), 'upper');
    assert.equal(jawForTooth('28'), 'upper');
    assert.equal(jawForTooth('31'), 'lower');
    assert.equal(jawForTooth('48'), 'lower');
});

test('the shared fixture round-trips through the editor representation', () => {
    const document = documentFromFixture();
    assert.deepEqual(Object.keys(document.upper).sort(), ['11', '26']);
    assert.deepEqual(Object.keys(document.lower).sort(), ['31', '48']);
    assert.deepEqual(document.upper['11'].incisal, [1.5, -2.25, 3.125]);
    assert.equal(document.upper['11'].cusps.length, 3);
});

test('basePlane survives an edit untouched', () => {
    // Not a landmark: a derived frame that rides along in the document and is never drawn.
    // Rebuilding the entry instead of copying it would drop it silently.
    const document = documentFromFixture();
    const before = JSON.parse(JSON.stringify(document.upper['11'].basePlane));
    place(document, { jaw: 'upper', tooth: '11', type: 'outer', point: [9, 9, 9] });
    remove(document, { jaw: 'upper', tooth: '11', type: 'incisal' });
    assert.deepEqual(document.upper['11'].basePlane, before);
});

test('a single-point type overwrites and reports what it displaced', () => {
    const document = emptyDocument();
    place(document, { jaw: 'upper', tooth: '11', type: 'incisal', point: [1, 1, 1] });
    const result = place(document, { jaw: 'upper', tooth: '11', type: 'incisal', point: [2, 2, 2] });
    assert.deepEqual(document.upper['11'].incisal, [2, 2, 2]);
    // Undo needs this, or it deletes a landmark the user never touched.
    assert.deepEqual(result.replaced, [1, 1, 1]);
});

test('a multi-point type appends and reports the index it landed at', () => {
    const document = emptyDocument();
    const first = place(document, { jaw: 'upper', tooth: '11', type: 'cusps', point: [1, 1, 1] });
    const second = place(document, { jaw: 'upper', tooth: '11', type: 'cusps', point: [2, 2, 2] });
    assert.equal(first.index, 0);
    assert.equal(second.index, 1);
    assert.equal(document.upper['11'].cusps.length, 2);
});

test('removing the last landmark drops the tooth rather than leaving an empty entry', () => {
    const document = emptyDocument();
    place(document, { jaw: 'upper', tooth: '11', type: 'incisal', point: [1, 1, 1] });
    remove(document, { jaw: 'upper', tooth: '11', type: 'incisal' });
    assert.deepEqual(document.upper, {});
});

test('removing something that is not there is a no-op, not a throw', () => {
    const document = emptyDocument();
    assert.equal(remove(document, { jaw: 'upper', tooth: '11', type: 'incisal' }), null);
    assert.equal(remove(document, { jaw: 'upper', tooth: '11', type: 'cusps', index: 4 }), null);
});

test('placement is refused for the wrong jaw, with a message naming the arch', () => {
    assert.equal(refusePlacement({ tooth: '11', type: 'incisal', hitJaw: 'upper' }), null);
    assert.equal(
        refusePlacement({ tooth: '11', type: 'incisal', hitJaw: 'lower' }),
        'Select the lower jaw tooth',
    );
});

test('placement is refused without a tooth, without a type, and for planar', () => {
    assert.equal(refusePlacement({ tooth: '', type: 'incisal' }), 'Select an FDI tooth');
    assert.equal(refusePlacement({ tooth: '11', type: null }), 'Select a landmark type');
    assert.match(refusePlacement({ tooth: '11', type: 'planar' }), /read-only/);
});

test('the per-tooth count adds single points and every cusp', () => {
    const document = documentFromFixture();
    // 8 single-point types + 3 cusps + 2 planar = 13. basePlane is not a landmark.
    assert.equal(countForTooth(document, '11'), 13);
    assert.equal(countForTooth(document, '17'), 0);
});

test('the save body always names both arches', () => {
    // A jaw the body omits is carried forward by the server, so an editor that sent only
    // the arch it had touched would find a cleared tooth quietly restored.
    const body = toSaveBody(emptyDocument(), 4);
    assert.deepEqual(body.meshes.map((mesh) => mesh.jaw), ['upper', 'lower']);
    assert.equal(body.expectedRevision, 4);
});

test('landmarks() is stable and skips malformed points', () => {
    const document = fromState({
        upper: { 11: { incisal: [1, 1, 1], outer: 'nope', cusps: [[2, 2, 2], [3, 3]] } },
        lower: {},
    });
    const found = landmarks(document);
    assert.deepEqual(found.map((entry) => entry.type), ['incisal', 'cusps']);
    assert.equal(found[1].index, 0);
});

test('identity distinguishes two cusps on one tooth', () => {
    const left = { jaw: 'upper', tooth: '11', type: 'cusps', index: 0 };
    assert.ok(sameLandmark(left, { ...left }));
    assert.ok(!sameLandmark(left, { ...left, index: 1 }));
});

test('cloneDocument is deep', () => {
    const document = documentFromFixture();
    const copy = cloneDocument(document);
    copy.upper['11'].incisal[0] = 999;
    assert.equal(document.upper['11'].incisal[0], 1.5);
});
