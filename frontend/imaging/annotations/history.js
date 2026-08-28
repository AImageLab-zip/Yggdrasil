/**
 * Undo/redo as an action log over the Yggdrasil representation.
 *
 * Ported from `static/js/intraoral_segmentation.js:601-717`, which Phase 5 deletes. The
 * roadmap's requirement is that it stay "implemented over the *Yggdrasil* representation,
 * not Cornerstone state", and this module is what keeps that true: it never touches a
 * viewer object. It mutates a `{FDI: [[[x, y], ...], ...]}` map, and the viewer is
 * redrawn from the map afterwards.
 *
 * That is not a stylistic preference. Cornerstone's annotation state is keyed by
 * `annotationUID`, the one identifier the governing rule says is never persisted; an
 * undo stack built on it would be a stack of references to objects that stop existing
 * when the tab is reloaded.
 *
 * ## Inverse by construction, not by snapshot
 *
 * Every action carries enough to invert itself -- create/remove at the same index,
 * delete stores the geometry it removed, move stores `from` and `to`. There is no
 * diffing and no full-state copy, so memory is O(edits) rather than O(document × edits).
 *
 * ## Positional identity, and the one thing that makes it safe
 *
 * Polygons and vertices have no ids, so correctness rests on `polygonIndex` and
 * `pointIndex` still meaning the same thing when an action is replayed. That holds
 * because the mutations are index-preserving splices **and because the redo stack is
 * cleared on any new action** -- without that clearing, a redo could target an index
 * that a subsequent edit had already moved.
 *
 * ## Two things fixed rather than ported
 *
 * The original stacks were global and were never rewritten when the RGB editor saved to
 * a new `FileRegistry` row, so an undo after an image edit targeted a stale file id.
 * {@link renameFile} exists for that. And the original never cleared the stacks when the
 * set of images changed, so this exposes {@link clearHistory} for a caller that
 * genuinely starts over.
 */

/** The original's cap, kept: a session's worth of edits, not a document's history. */
export const MAX_HISTORY = 100;

/** The action types the replay understands. An unknown one is an error, not a skip. */
export const ACTION_TYPES = Object.freeze([
    'polygon-create',
    'polygon-delete',
    'vertex-insert',
    'vertex-delete',
    'vertex-move',
]);

/** A fresh, empty history. */
export function createHistory() {
    return { undo: [], redo: [] };
}

/**
 * Record one action, and drop any redo it invalidates.
 *
 * Clearing redo is load-bearing rather than conventional -- see the header: the indices a
 * redo action names would otherwise be interpreted against a document that has moved.
 *
 * @param {object} history
 * @param {object} action
 * @returns {object} the same history, mutated.
 */
export function record(history, action) {
    if (!ACTION_TYPES.includes(action?.type)) {
        throw new Error(`Unknown history action ${JSON.stringify(action?.type)}.`);
    }
    history.undo.push(action);
    if (history.undo.length > MAX_HISTORY) {
        history.undo.shift();
    }
    history.redo = [];
    return history;
}

/** Whether there is anything to undo / redo, for enabling the buttons. */
export function canUndo(history) {
    return history.undo.length > 0;
}

export function canRedo(history) {
    return history.redo.length > 0;
}

/**
 * Discard every action belonging to one drawing session.
 *
 * The original's collapse rule, and it is a real behaviour rather than housekeeping:
 * while a polygon is being drawn, each click is its own action, but once it closes the
 * whole polygon becomes a single `polygon-create`. One undo after closing therefore
 * removes the polygon rather than un-clicking its last vertex, which is what a user
 * expects and what they would not get from a naive log.
 *
 * @param {object} history
 * @param {number|string} draftId
 */
export function dropDraft(history, draftId) {
    const keep = (action) => action.draftId !== draftId;
    history.undo = history.undo.filter(keep);
    history.redo = history.redo.filter(keep);
    return history;
}

/**
 * Re-point every action at a file that has been replaced by a new row.
 *
 * The RGB editor writes a *new* `FileRegistry` row when its edits are confirmed, so the
 * work the user just did is suddenly filed under an id no action mentions. The original
 * did not do this, so the first undo after an image edit silently targeted a file that
 * was no longer on screen.
 *
 * @param {object} history
 * @param {number} fromFileId
 * @param {number} toFileId
 */
export function renameFile(history, fromFileId, toFileId) {
    const move = (action) =>
        action.fileId === fromFileId ? { ...action, fileId: toFileId } : action;
    history.undo = history.undo.map(move);
    history.redo = history.redo.map(move);
    return history;
}

/**
 * The actions that turn one polygon into another.
 *
 * Exists because Cornerstone reports *that* an annotation changed, never *how*: an
 * `ANNOTATION_MODIFIED` event carries the new state and nothing else. The action log needs
 * the how, so the difference is derived here -- in the module that owns the action
 * vocabulary, rather than in a viewer module that would end up inventing a fifth action
 * type nothing can invert.
 *
 * Three cases are recognised because they are the three a contour tool actually produces:
 * dragging a handle, adding a control point, and deleting one. Anything else -- and the
 * only realistic source is a programmatic replacement -- becomes a delete followed by a
 * create, which is expressible, inverts correctly, and costs the user two undo steps for
 * an edit they cannot make by hand anyway. The alternative was a `polygon-replace` action
 * whose inverse would have to carry a whole second copy of the geometry.
 *
 * @param {number[][]} previous
 * @param {number[][]} next
 * @param {object} at `{fileId, tooth, polygonIndex}`
 * @returns {object[]} zero actions when the polygons are equal.
 */
export function actionsBetween(previous, next, at) {
    const before = previous ?? [];
    const after = next ?? [];
    if (samePolygon(before, after)) {
        return [];
    }

    if (before.length === after.length) {
        const moved = [];
        for (let index = 0; index < before.length; index += 1) {
            if (!samePoint(before[index], after[index])) {
                moved.push(index);
            }
        }
        // One handle at a time is what dragging produces. Two or more differing points
        // means something replaced the ring, and pretending otherwise would record an
        // inverse that restores only one of them.
        if (moved.length === 1) {
            const [pointIndex] = moved;
            return [
                {
                    type: 'vertex-move',
                    ...at,
                    pointIndex,
                    from: clonePolygon([before[pointIndex]])[0],
                    to: clonePolygon([after[pointIndex]])[0],
                },
            ];
        }
    }

    if (after.length === before.length + 1) {
        const pointIndex = firstDivergence(before, after);
        if (pointIndex !== null && samePolygon(before, withoutIndex(after, pointIndex))) {
            return [
                {
                    type: 'vertex-insert',
                    ...at,
                    pointIndex,
                    point: clonePolygon([after[pointIndex]])[0],
                },
            ];
        }
    }

    if (after.length === before.length - 1) {
        const pointIndex = firstDivergence(after, before);
        if (pointIndex !== null && samePolygon(after, withoutIndex(before, pointIndex))) {
            return [
                {
                    type: 'vertex-delete',
                    ...at,
                    pointIndex,
                    point: clonePolygon([before[pointIndex]])[0],
                },
            ];
        }
    }

    return [
        { type: 'polygon-delete', ...at, polygon: clonePolygon(before) },
        { type: 'polygon-create', ...at, polygon: clonePolygon(after) },
    ];
}

function samePoint(left, right) {
    return Boolean(left) && Boolean(right) && left[0] === right[0] && left[1] === right[1];
}

function samePolygon(left, right) {
    return (
        left.length === right.length &&
        left.every((point, index) => samePoint(point, right[index]))
    );
}

/** The first index at which `longer` stops matching `shorter`, or its end. */
function firstDivergence(shorter, longer) {
    for (let index = 0; index < shorter.length; index += 1) {
        if (!samePoint(shorter[index], longer[index])) {
            return index;
        }
    }
    return shorter.length;
}

function withoutIndex(polygon, index) {
    return polygon.filter((_point, position) => position !== index);
}

/** Forget everything. For a caller that has genuinely started over. */
export function clearHistory(history) {
    history.undo = [];
    history.redo = [];
    return history;
}

// ---------------------------------------------------------------------------
// The representation the actions operate over
// ---------------------------------------------------------------------------

function polygonsFor(teethByFile, fileId, tooth) {
    const teeth = (teethByFile[fileId] ??= {});
    return (teeth[String(tooth)] ??= []);
}

function dropToothIfEmpty(teethByFile, fileId, tooth) {
    const teeth = teethByFile[fileId];
    if (teeth && teeth[String(tooth)]?.length === 0) {
        delete teeth[String(tooth)];
    }
}

/** Insert a polygon at an index, recreating the tooth key if it was dropped. */
export function insertPolygon(teethByFile, { fileId, tooth, polygonIndex, polygon }) {
    polygonsFor(teethByFile, fileId, tooth).splice(polygonIndex, 0, clonePolygon(polygon));
}

/** Remove a polygon by index, dropping the tooth key when it empties. */
export function removePolygon(teethByFile, { fileId, tooth, polygonIndex }) {
    const polygons = teethByFile[fileId]?.[String(tooth)];
    if (!polygons) return;
    polygons.splice(polygonIndex, 1);
    dropToothIfEmpty(teethByFile, fileId, tooth);
}

export function insertVertex(teethByFile, { fileId, tooth, polygonIndex, pointIndex, point }) {
    const polygon = teethByFile[fileId]?.[String(tooth)]?.[polygonIndex];
    if (!polygon) return;
    polygon.splice(pointIndex, 0, [Number(point[0]), Number(point[1])]);
}

export function removeVertex(teethByFile, { fileId, tooth, polygonIndex, pointIndex }) {
    const polygon = teethByFile[fileId]?.[String(tooth)]?.[polygonIndex];
    if (!polygon) return;
    polygon.splice(pointIndex, 1);
}

export function setVertex(teethByFile, { fileId, tooth, polygonIndex, pointIndex }, point) {
    const polygon = teethByFile[fileId]?.[String(tooth)]?.[polygonIndex];
    if (!polygon || !polygon[pointIndex]) return;
    polygon[pointIndex] = [Number(point[0]), Number(point[1])];
}

function clonePolygon(polygon) {
    return polygon.map((point) => [Number(point[0]), Number(point[1])]);
}

/**
 * Apply one action in one direction against the teeth map.
 *
 * @param {object} teethByFile `{[fileId]: {[fdi]: [[[x, y], ...], ...]}}`, mutated.
 * @param {object} action
 * @param {'undo'|'redo'} direction
 */
export function applyAction(teethByFile, action, direction) {
    const undo = direction === 'undo';
    switch (action.type) {
        case 'polygon-create':
            (undo ? removePolygon : insertPolygon)(teethByFile, action);
            return;
        case 'polygon-delete':
            (undo ? insertPolygon : removePolygon)(teethByFile, action);
            return;
        case 'vertex-insert':
            (undo ? removeVertex : insertVertex)(teethByFile, action);
            return;
        case 'vertex-delete':
            (undo ? insertVertex : removeVertex)(teethByFile, action);
            return;
        case 'vertex-move':
            setVertex(teethByFile, action, undo ? action.from : action.to);
            return;
        default:
            throw new Error(`Unknown history action ${JSON.stringify(action.type)}.`);
    }
}

/**
 * Undo one action.
 *
 * @returns {object|null} the action undone, so the caller knows which file to refocus
 *   and redraw -- an action can belong to an image that is not the one on screen.
 */
export function undo(history, teethByFile) {
    const action = history.undo.pop();
    if (!action) return null;
    applyAction(teethByFile, action, 'undo');
    history.redo.push(action);
    return action;
}

/** Redo one action. @returns {object|null} the action redone. */
export function redo(history, teethByFile) {
    const action = history.redo.pop();
    if (!action) return null;
    applyAction(teethByFile, action, 'redo');
    history.undo.push(action);
    return action;
}
