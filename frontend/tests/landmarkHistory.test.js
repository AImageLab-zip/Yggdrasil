import test from 'node:test';
import assert from 'node:assert/strict';

import {
    canRedo,
    canUndo,
    clearHistory,
    createHistory,
    placeAndRecord,
    record,
    redo,
    removeAndRecord,
    undo,
} from '../imaging/mesh/landmarkHistory.js';
import { emptyDocument, place } from '../imaging/mesh/landmarkDocument.js';
import { MAX_ACTIONS } from '../imaging/annotations/actionLog.js';

/**
 * Undo *and redo* for landmarks.
 *
 * The legacy tool had undo only, over fifty deep copies of the whole document. The roadmap
 * lists that as one of the inconsistencies this migration closes, so redo is a feature
 * under test rather than a side effect.
 */

function documentWith(...points) {
    const document = emptyDocument();
    for (const point of points) {
        place(document, { jaw: 'upper', tooth: '11', type: 'cusps', point });
    }
    return document;
}

test('placing then undoing leaves the document as it was', () => {
    const document = emptyDocument();
    const history = createHistory();
    placeAndRecord(history, document, {
        jaw: 'upper', tooth: '11', type: 'incisal', point: [1, 2, 3],
    });
    assert.ok(canUndo(history));
    undo(history, document);
    assert.deepEqual(document.upper, {});
    assert.ok(canRedo(history));
});

test('redo puts it back in the same slot', () => {
    const document = documentWith([1, 1, 1], [2, 2, 2]);
    const history = createHistory();
    placeAndRecord(history, document, {
        jaw: 'upper', tooth: '11', type: 'cusps', point: [3, 3, 3],
    });
    undo(history, document);
    assert.equal(document.upper['11'].cusps.length, 2);
    redo(history, document);
    assert.deepEqual(document.upper['11'].cusps, [[1, 1, 1], [2, 2, 2], [3, 3, 3]]);
});

test('undoing a place that overwrote restores what it displaced', () => {
    // The difference between "put it back how it was" and "leave a hole". The snapshot
    // stack got this right only because it copied everything; an action log has to carry
    // the displaced value, which is why `place` reports it.
    const document = emptyDocument();
    const history = createHistory();
    place(document, { jaw: 'upper', tooth: '11', type: 'incisal', point: [1, 1, 1] });
    placeAndRecord(history, document, {
        jaw: 'upper', tooth: '11', type: 'incisal', point: [9, 9, 9],
    });
    undo(history, document);
    assert.deepEqual(document.upper['11'].incisal, [1, 1, 1]);
});

test('deleting then undoing restores the point at its index', () => {
    const document = documentWith([1, 1, 1], [2, 2, 2], [3, 3, 3]);
    const history = createHistory();
    removeAndRecord(history, document, {
        jaw: 'upper', tooth: '11', type: 'cusps', index: 1,
    });
    assert.deepEqual(document.upper['11'].cusps, [[1, 1, 1], [3, 3, 3]]);
    undo(history, document);
    assert.deepEqual(document.upper['11'].cusps, [[1, 1, 1], [2, 2, 2], [3, 3, 3]]);
});

test('deleting nothing records nothing', () => {
    const document = emptyDocument();
    const history = createHistory();
    assert.equal(removeAndRecord(history, document, {
        jaw: 'upper', tooth: '11', type: 'incisal',
    }), null);
    assert.ok(!canUndo(history));
});

test('a new action clears redo', () => {
    // Load-bearing: actions name a cusp by index, so a redo left standing after an
    // unrelated edit would be replayed against a list whose indices have moved.
    const document = emptyDocument();
    const history = createHistory();
    placeAndRecord(history, document, { jaw: 'upper', tooth: '11', type: 'cusps', point: [1, 1, 1] });
    undo(history, document);
    assert.ok(canRedo(history));
    placeAndRecord(history, document, { jaw: 'upper', tooth: '11', type: 'cusps', point: [2, 2, 2] });
    assert.ok(!canRedo(history));
});

test('the stack is capped and drops from the front', () => {
    const document = emptyDocument();
    const history = createHistory();
    for (let index = 0; index < MAX_ACTIONS + 10; index += 1) {
        placeAndRecord(history, document, {
            jaw: 'upper', tooth: '11', type: 'cusps', point: [index, 0, 0],
        });
    }
    assert.equal(history.undo.length, MAX_ACTIONS);
    // The oldest actions went, not the newest.
    assert.equal(history.undo[history.undo.length - 1].point[0], MAX_ACTIONS + 9);
});

test('an unknown action type is refused rather than silently skipped', () => {
    // A silently ignored action is an undo the user presses and watches do nothing.
    assert.throws(() => record(createHistory(), { type: 'nonsense' }), /Unknown history action/);
});

test('clearing forgets both stacks', () => {
    const document = emptyDocument();
    const history = createHistory();
    placeAndRecord(history, document, { jaw: 'upper', tooth: '11', type: 'cusps', point: [1, 1, 1] });
    undo(history, document);
    clearHistory(history);
    assert.ok(!canUndo(history));
    assert.ok(!canRedo(history));
});

test('undo on an empty stack is null, not a throw', () => {
    assert.equal(undo(createHistory(), emptyDocument()), null);
    assert.equal(redo(createHistory(), emptyDocument()), null);
});

test('the action reports which tooth it touched, so the caller can refocus', () => {
    const document = emptyDocument();
    const history = createHistory();
    placeAndRecord(history, document, { jaw: 'lower', tooth: '31', type: 'gingival', point: [0, 0, 0] });
    assert.equal(undo(history, document).at.tooth, '31');
});
