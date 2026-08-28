/**
 * The undo/redo mechanics, with no opinion about what is being edited.
 *
 * Extracted when IOS landmarks became the second surface to need it. The two vocabularies
 * have nothing in common -- one moves polygon vertices on a photograph, the other places
 * points on a mesh -- but "push, cap, clear redo on a new action, pop and invert" is the
 * same problem twice, and the part that is easy to get subtly wrong is the part that is
 * identical.
 *
 * Specifically: **clearing redo on every new action is load-bearing, not tidiness.** Both
 * surfaces identify what an action refers to positionally (a polygon index, a cusp index),
 * so a redo left standing after an unrelated edit would be replayed against a document
 * whose indices have moved. It is the kind of rule that survives review once and then dies
 * quietly in the second copy.
 *
 * A caller supplies its own action vocabulary and its own `apply`; this module never looks
 * inside an action beyond its `type`.
 */

/** A session's worth of edits, not a document's history. */
export const MAX_ACTIONS = 100;

/** A fresh, empty log. */
export function createLog() {
    return { undo: [], redo: [] };
}

/**
 * Record one action, and drop any redo it invalidates.
 *
 * @param {object} log
 * @param {object} action must carry a `type` the vocabulary knows.
 * @param {object} options `{types, max}` -- `types` is the caller's vocabulary; an unknown
 *   type is an error rather than a skip, because a silently ignored action is an undo the
 *   user will press and watch do nothing.
 */
export function record(log, action, { types, max = MAX_ACTIONS } = {}) {
    if (types && !types.includes(action?.type)) {
        throw new Error(`Unknown history action ${JSON.stringify(action?.type)}.`);
    }
    log.undo.push(action);
    if (log.undo.length > max) {
        log.undo.shift();
    }
    log.redo = [];
    return log;
}

export function canUndo(log) {
    return log.undo.length > 0;
}

export function canRedo(log) {
    return log.redo.length > 0;
}

/** Forget everything. For a caller that has genuinely started over. */
export function clear(log) {
    log.undo = [];
    log.redo = [];
    return log;
}

/**
 * Undo one action against `state`.
 *
 * @param {object} log
 * @param {object} state the caller's representation, mutated by `apply`.
 * @param {(state: object, action: object, direction: 'undo'|'redo') => void} apply
 * @returns {object|null} the action undone, so the caller knows what to redraw -- an
 *   action can belong to something that is not currently on screen.
 */
export function undo(log, state, apply) {
    const action = log.undo.pop();
    if (!action) return null;
    apply(state, action, 'undo');
    log.redo.push(action);
    return action;
}

/** Redo one action. @returns {object|null} the action redone. */
export function redo(log, state, apply) {
    const action = log.redo.pop();
    if (!action) return null;
    apply(state, action, 'redo');
    log.undo.push(action);
    return action;
}
