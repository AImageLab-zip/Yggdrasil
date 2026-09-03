import test from 'node:test';
import assert from 'node:assert/strict';

import {
    MAX_HISTORY,
    applyAction,
    canRedo,
    canUndo,
    clearHistory,
    createHistory,
    dropDraft,
    record,
    redo,
    renameFile,
    undo,
} from '../imaging/annotations/history.js';

/** `{[fileId]: {[fdi]: [[[x, y], ...], ...]}}` */
function teeth() {
    return { 5: { 36: [[[0, 0], [10, 0], [10, 10]]] } };
}

function create(overrides = {}) {
    return {
        type: 'polygon-create',
        fileId: 5,
        tooth: '36',
        polygonIndex: 1,
        polygon: [[20, 20], [30, 20], [30, 30]],
        ...overrides,
    };
}

// ---------------------------------------------------------------------------
// The representation it operates over
// ---------------------------------------------------------------------------

test('the log never touches a viewer object, only the teeth map', () => {
    // The roadmap's requirement: implemented over the Yggdrasil representation, not
    // Cornerstone state. Not stylistic -- Cornerstone's annotation state is keyed by
    // annotationUID, the one identifier that is never persisted, so a stack built on it
    // would hold references to objects that stop existing on reload.
    const map = teeth();
    applyAction(map, create(), 'redo');
    assert.equal(map[5]['36'].length, 2);
    assert.deepEqual(map[5]['36'][1], [[20, 20], [30, 20], [30, 30]]);
});

test('every action inverts itself without a snapshot', () => {
    // Inverse by construction: create/remove at the same index, delete stores what it
    // removed, move stores from and to. Memory is O(edits), not O(document x edits).
    const cases = [
        create(),
        { type: 'polygon-delete', fileId: 5, tooth: '36', polygonIndex: 0, polygon: [[0, 0], [10, 0], [10, 10]] },
        { type: 'vertex-insert', fileId: 5, tooth: '36', polygonIndex: 0, pointIndex: 1, point: [5, 5] },
        { type: 'vertex-delete', fileId: 5, tooth: '36', polygonIndex: 0, pointIndex: 1, point: [10, 0] },
        { type: 'vertex-move', fileId: 5, tooth: '36', polygonIndex: 0, pointIndex: 0, from: [0, 0], to: [1, 1] },
    ];
    for (const action of cases) {
        const map = teeth();
        const before = JSON.stringify(map);
        applyAction(map, action, 'redo');
        applyAction(map, action, 'undo');
        assert.equal(JSON.stringify(map), before, action.type);
    }
});

test('removing the last polygon drops the tooth key, and undo recreates it', () => {
    // The one place positional identity is fragile, and it does recover: removePolygon
    // deletes the key when the array empties, and insertPolygon recreates it.
    const map = teeth();
    const action = { type: 'polygon-delete', fileId: 5, tooth: '36', polygonIndex: 0, polygon: [[0, 0], [10, 0], [10, 10]] };
    applyAction(map, action, 'redo');
    assert.ok(!('36' in map[5]), 'an empty tooth must not linger as an empty array');
    applyAction(map, action, 'undo');
    assert.deepEqual(map[5]['36'], [[[0, 0], [10, 0], [10, 10]]]);
});

test('an unknown action type is an error, not a silent skip', () => {
    assert.throws(() => applyAction(teeth(), { type: 'nope' }, 'undo'), /Unknown history action/);
    assert.throws(() => record(createHistory(), { type: 'nope' }), /Unknown history action/);
});

// ---------------------------------------------------------------------------
// The stacks
// ---------------------------------------------------------------------------

test('undo moves an action to redo, and redo moves it back', () => {
    const history = record(createHistory(), create());
    const map = teeth();
    applyAction(map, create(), 'redo');

    assert.equal(canUndo(history), true);
    assert.equal(canRedo(history), false);

    assert.equal(undo(history, map).type, 'polygon-create');
    assert.equal(map[5]['36'].length, 1);
    assert.equal(canRedo(history), true);

    assert.equal(redo(history, map).type, 'polygon-create');
    assert.equal(map[5]['36'].length, 2);
});

test('undo on an empty stack returns null rather than throwing', () => {
    const history = createHistory();
    assert.equal(undo(history, teeth()), null);
    assert.equal(redo(history, teeth()), null);
});

test('recording clears redo, which is what keeps indices meaningful', () => {
    // Load-bearing rather than conventional: polygons and vertices have no ids, so a
    // redo names an index. Without clearing, that index would be interpreted against a
    // document a subsequent edit had already moved.
    const history = record(createHistory(), create());
    const map = teeth();
    applyAction(map, create(), 'redo');
    undo(history, map);
    assert.equal(canRedo(history), true);

    record(history, create({ polygonIndex: 5 }));
    assert.equal(canRedo(history), false, 'the stale redo must not survive a new edit');
});

test('the stack is capped and drops the oldest, not the newest', () => {
    const history = createHistory();
    for (let index = 0; index < MAX_HISTORY + 10; index += 1) {
        record(history, create({ polygonIndex: index }));
    }
    assert.equal(history.undo.length, MAX_HISTORY);
    assert.equal(
        history.undo[history.undo.length - 1].polygonIndex,
        MAX_HISTORY + 9,
        'the most recent edit must always be undoable'
    );
    assert.equal(history.undo[0].polygonIndex, 10);
});

// ---------------------------------------------------------------------------
// The draft collapse rule
// ---------------------------------------------------------------------------

test('closing a polygon collapses its clicks into one undoable step', () => {
    // A real behaviour, not housekeeping: while drawing, each click is its own action,
    // but one undo after closing must remove the polygon rather than un-click its last
    // vertex, which is what a user expects and what a naive log would not give them.
    const history = createHistory();
    record(history, { ...create({ polygonIndex: 0 }), type: 'vertex-insert', pointIndex: 0, point: [0, 0], draftId: 7 });
    record(history, { ...create({ polygonIndex: 0 }), type: 'vertex-insert', pointIndex: 1, point: [1, 1], draftId: 7 });
    assert.equal(history.undo.length, 2);

    dropDraft(history, 7);
    record(history, create());
    assert.equal(history.undo.length, 1);
    assert.equal(history.undo[0].type, 'polygon-create');
});

test('dropping one draft leaves another alone', () => {
    const history = createHistory();
    record(history, { ...create(), type: 'vertex-insert', pointIndex: 0, point: [0, 0], draftId: 1 });
    record(history, { ...create(), type: 'vertex-insert', pointIndex: 0, point: [0, 0], draftId: 2 });
    dropDraft(history, 1);
    assert.equal(history.undo.length, 1);
    assert.equal(history.undo[0].draftId, 2);
});

// ---------------------------------------------------------------------------
// The two bugs fixed rather than ported
// ---------------------------------------------------------------------------

test('renameFile re-points actions after the editor writes a new row', () => {
    // The RGB editor writes a NEW FileRegistry row when its edits are confirmed, so the
    // work the user just did is filed under an id no action mentions. The original never
    // did this, so the first undo after an image edit silently targeted a file that was
    // no longer on screen.
    const history = record(createHistory(), create({ fileId: 5 }));
    renameFile(history, 5, 9);
    assert.equal(history.undo[0].fileId, 9);
});

test('renameFile leaves other files alone and covers the redo stack too', () => {
    const history = createHistory();
    record(history, create({ fileId: 5 }));
    record(history, create({ fileId: 6 }));
    const map = { 5: {}, 6: {} };
    undo(history, map);

    renameFile(history, 6, 9);
    assert.equal(history.undo[0].fileId, 5, 'a different file must not move');
    assert.equal(history.redo[0].fileId, 9, 'a pending redo must move with it');
});

test('clearHistory forgets both stacks', () => {
    const history = record(createHistory(), create());
    undo(history, teeth());
    clearHistory(history);
    assert.equal(canUndo(history), false);
    assert.equal(canRedo(history), false);
});

test('an action can belong to a file that is not on screen, and says so', () => {
    // The return value is what lets the caller refocus and redraw the right image rather
    // than applying the change invisibly to a study nobody is looking at.
    const history = record(createHistory(), create({ fileId: 99 }));
    const map = { 99: { 36: [[[0, 0], [1, 0], [1, 1]], [[20, 20], [30, 20], [30, 30]]] } };
    assert.equal(undo(history, map).fileId, 99);
});
