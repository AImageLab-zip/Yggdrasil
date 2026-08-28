/**
 * `actionsBetween`: what Cornerstone did not tell us.
 *
 * An `ANNOTATION_MODIFIED` event carries the new state and nothing else -- never *how* it
 * changed -- so the action log's entry has to be derived. Every case below has to invert
 * cleanly through `applyAction`, because an action that does not is an undo that silently
 * leaves the document in a state the user never made.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
    ACTION_TYPES,
    actionsBetween,
    applyAction,
    createHistory,
    record,
    redo,
    undo,
} from '../imaging/annotations/history.js';

const AT = { fileId: 7, tooth: '36', polygonIndex: 0 };

const RING = [
    [10, 10],
    [30, 10],
    [30, 30],
    [10, 30],
];

/** Apply the derived actions to a document and hand back what it became. */
function replay(before, after) {
    const document_ = { 7: { 36: [before.map((point) => [...point])] } };
    const history = createHistory();
    for (const action of actionsBetween(before, after, AT)) {
        record(history, action);
        applyAction(document_, action, 'redo');
    }
    return { document_, history };
}

describe('actionsBetween', () => {
    it('is empty when nothing moved', () => {
        assert.deepEqual(actionsBetween(RING, RING.map((point) => [...point]), AT), []);
    });

    it('one dragged handle is one vertex-move carrying both positions', () => {
        const after = RING.map((point, index) => (index === 2 ? [99, 99] : [...point]));
        const [action, ...rest] = actionsBetween(RING, after, AT);
        assert.deepEqual(rest, []);
        assert.equal(action.type, 'vertex-move');
        assert.equal(action.pointIndex, 2);
        assert.deepEqual(action.from, [30, 30]);
        assert.deepEqual(action.to, [99, 99]);
    });

    it('a control point added mid-ring is one vertex-insert at its index', () => {
        const after = [...RING.slice(0, 2), [40, 20], ...RING.slice(2)];
        const [action, ...rest] = actionsBetween(RING, after, AT);
        assert.deepEqual(rest, []);
        assert.equal(action.type, 'vertex-insert');
        assert.equal(action.pointIndex, 2);
        assert.deepEqual(action.point, [40, 20]);
    });

    it('a control point appended at the end is still one insert', () => {
        // The divergence scan runs out before finding a mismatch, so the index has to fall
        // through to the end rather than reporting "no difference".
        const after = [...RING, [5, 20]];
        const [action] = actionsBetween(RING, after, AT);
        assert.equal(action.type, 'vertex-insert');
        assert.equal(action.pointIndex, RING.length);
    });

    it('a deleted control point is one vertex-delete carrying its geometry', () => {
        const after = RING.filter((_point, index) => index !== 1);
        const [action, ...rest] = actionsBetween(RING, after, AT);
        assert.deepEqual(rest, []);
        assert.equal(action.type, 'vertex-delete');
        assert.equal(action.pointIndex, 1);
        assert.deepEqual(action.point, [30, 10]);
    });

    it('anything else becomes a delete and a create, which still inverts', () => {
        // Two handles cannot move at once by hand. Recording a single vertex-move would
        // write an inverse that restored only one of them.
        const after = [
            [0, 0],
            [1, 1],
            [2, 2],
            [3, 3],
            [4, 4],
            [5, 5],
        ];
        const actions = actionsBetween(RING, after, AT);
        assert.deepEqual(
            actions.map((action) => action.type),
            ['polygon-delete', 'polygon-create']
        );
        assert.deepEqual(actions[0].polygon, RING);
        assert.deepEqual(actions[1].polygon, after);
    });

    it('only ever emits action types the replay understands', () => {
        const cases = [
            [RING, RING.map((point, index) => (index ? point : [0, 0]))],
            [RING, [...RING, [1, 1]]],
            [RING, RING.slice(1)],
            [RING, [[9, 9], [8, 8], [7, 7]]],
        ];
        for (const [before, after] of cases) {
            for (const action of actionsBetween(before, after, AT)) {
                assert.ok(ACTION_TYPES.includes(action.type), action.type);
            }
        }
    });
});

describe('the derived actions invert', () => {
    const cases = [
        ['a move', RING.map((point, index) => (index === 2 ? [99, 99] : point))],
        ['an insert', [...RING.slice(0, 2), [40, 20], ...RING.slice(2)]],
        ['a delete', RING.filter((_point, index) => index !== 1)],
        ['a wholesale replacement', [[9, 9], [8, 8], [7, 7], [6, 6], [5, 5]]],
    ];

    for (const [name, after] of cases) {
        it(`undo restores the original after ${name}`, () => {
            const { document_, history } = replay(RING, after);
            assert.deepEqual(document_[7][36][0], after, 'redo produced the new shape');

            while (history.undo.length) {
                undo(history, document_);
            }
            assert.deepEqual(document_[7][36][0], RING);
        });

        it(`redo reproduces the change after ${name}`, () => {
            const { document_, history } = replay(RING, after);
            while (history.undo.length) {
                undo(history, document_);
            }
            while (history.redo.length) {
                redo(history, document_);
            }
            assert.deepEqual(document_[7][36][0], after);
        });
    }
});
