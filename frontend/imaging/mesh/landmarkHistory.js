/**
 * Undo *and redo* for landmark placement.
 *
 * The legacy tool had undo only, over a stack of fifty deep copies of the whole document
 * (`ios.js:624-631`). `docs/cornerstone-roadmap.md` lists that inconsistency as one of the
 * defects this migration exists to close -- intraoral had a full action log, IOS had
 * snapshots, laparoscopy had nothing.
 *
 * This is the action log, over the Yggdrasil representation as decision #4 requires: it
 * mutates the landmark document and the viewport is redrawn from it. It never holds a vtk
 * actor, which matters because marker actors are rebuilt on every redraw -- a stack of
 * references to them would be a stack of objects that stop existing.
 *
 * Inverse by construction: a place stores the index it landed at and whatever
 * single-point value it displaced, a delete stores the point it removed. So memory is
 * O(edits), not O(document x edits), and an undo restores an overwritten landmark instead
 * of leaving a hole where one used to be -- which the snapshot stack got right only
 * because it copied everything.
 */

import * as log from '../annotations/actionLog.js';
import { place, remove } from './landmarkDocument.js';

export const ACTION_TYPES = Object.freeze(['landmark-place', 'landmark-delete']);

export const createHistory = log.createLog;
export const canUndo = log.canUndo;
export const canRedo = log.canRedo;
export const clearHistory = log.clear;

/** Record one action, dropping any redo it invalidates. */
export function record(history, action) {
    return log.record(history, action, { types: ACTION_TYPES });
}

/**
 * Place a landmark and record it in one step.
 *
 * The pair exists so a caller cannot do one without the other -- the legacy code pushed
 * its undo snapshot from the click handler, and any future placement path would have had
 * to remember to.
 *
 * @returns {object} the action recorded, which carries the resolved index.
 */
export function placeAndRecord(history, document, at) {
    const { index, replaced } = place(document, at);
    const action = {
        type: 'landmark-place',
        // Nested rather than spread: a landmark's own `type` is `incisal`, `cusps` and so
        // on, which spread flat would overwrite the action's discriminator and make every
        // action unrecognisable. The two vocabularies genuinely collide on that word.
        at: { jaw: at.jaw, tooth: at.tooth, type: at.type, index },
        point: [...at.point],
        replaced,
    };
    record(history, action);
    return action;
}

/** Delete a landmark and record it. @returns {object|null} null when nothing was there. */
export function removeAndRecord(history, document, at) {
    const removed = remove(document, at);
    if (removed === null) return null;
    const action = {
        type: 'landmark-delete',
        at: { jaw: at.jaw, tooth: at.tooth, type: at.type, index: at.index ?? null },
        point: [...removed],
    };
    record(history, action);
    return action;
}

/**
 * Apply one action in one direction.
 *
 * Undoing a place that *overwrote* a single-point landmark restores the displaced value
 * rather than deleting the slot -- the difference between "put it back how it was" and
 * "leave a hole", and the reason `place` reports what it replaced.
 */
export function applyAction(document, action, direction) {
    const undoing = direction === 'undo';
    const { at, point, replaced } = action;
    switch (action.type) {
        case 'landmark-place':
            if (undoing) {
                remove(document, at);
                if (replaced) place(document, { ...at, index: null, point: replaced });
            } else {
                place(document, { ...at, point });
            }
            return;
        case 'landmark-delete':
            if (undoing) place(document, { ...at, point });
            else remove(document, at);
            return;
        default:
            throw new Error(`Unknown history action ${JSON.stringify(action.type)}.`);
    }
}

/** @returns {object|null} the action undone, so the caller knows which tooth to refocus. */
export function undo(history, document) {
    return log.undo(history, document, applyAction);
}

/** @returns {object|null} the action redone. */
export function redo(history, document) {
    return log.redo(history, document, applyAction);
}
