/**
 * The IOS landmark document: what a landmark is, and where it may go.
 *
 * The one module that knows the shape. Everything else -- the viewport, the markers, the
 * controls -- asks this. Ported from `static/js/modality_viewers/ios.js`, which Phase 6
 * deletes, and kept pure so it can be tested without a GPU or a DOM.
 *
 * ## The wire format is not the storage format
 *
 * The legacy document keyed each tooth as `<patientId>_<jaw>_FDI_<tooth>`, which repeats
 * two facts the request already carries: the patient, and an arch the FDI quadrant
 * determines. The endpoint added in PR 6.1 takes `{jaw, landmarks: {FDI: entry}}` instead,
 * so there is nothing for a client to contradict. This module works in that FDI-keyed
 * form; the server renders the legacy keys for the export.
 *
 * ## Two kinds of landmark, and one that is not editable
 *
 * Eight types hold a single point and are overwritten when placed again. `cusps` and
 * `planar` hold lists and append. `planar` is machine-produced -- it comes from the
 * landmark job, not from a person -- so it renders and it is never placed or deleted by
 * hand. `basePlane` is not a landmark at all: it is a derived frame that rides along in
 * the document, is never drawn, and must survive an edit untouched, which is why every
 * mutation here copies rather than rebuilds.
 */

/** The ten landmark types, in the order the workbench lists them. */
export const LANDMARK_TYPES = Object.freeze([
    'incisal', 'outer', 'bracket', 'gingival', 'mesial', 'distal', 'inner', 'facial',
    'cusps', 'planar',
]);

/** The types that hold a list of points rather than one. */
export const MULTI_POINT_TYPES = Object.freeze(['cusps', 'planar']);

/** `planar` is the landmark job's output; a person reads it and never places it. */
export const EDITABLE_TYPES = Object.freeze(LANDMARK_TYPES.filter((type) => type !== 'planar'));

/** Labels for the workbench. `planar` has none because it is never offered as a choice. */
export const TYPE_LABELS = Object.freeze({
    incisal: 'Incisal', outer: 'Outer', bracket: 'Bracket', gingival: 'Gingival',
    mesial: 'Mesial', distal: 'Distal', inner: 'Inner', facial: 'Facial', cusps: 'Cusps',
});

/** Marker colours, carried over verbatim: clinicians have learned them. */
export const TYPE_COLORS = Object.freeze({
    incisal: 0xf97316, outer: 0x2563eb, bracket: 0x7c3aed, gingival: 0xdc2626,
    mesial: 0x16a34a, distal: 0x0891b2, inner: 0x4f46e5, facial: 0xdb2777,
    cusps: 0xca8a04, planar: 0x64748b,
});

/** The 32 permanent teeth in *mouth* order, which is the order the grid is drawn in. */
export const TEETH = Object.freeze([
    '18', '17', '16', '15', '14', '13', '12', '11',
    '21', '22', '23', '24', '25', '26', '27', '28',
    '48', '47', '46', '45', '44', '43', '42', '41',
    '31', '32', '33', '34', '35', '36', '37', '38',
]);

/** The arches, upper first. */
export const JAWS = Object.freeze(['upper', 'lower']);

/** The cap the server enforces on a multi-point type, mirrored so the UI refuses first. */
export const MAX_POINTS_PER_TYPE = 500;

export function isMultiPoint(type) {
    return MULTI_POINT_TYPES.includes(type);
}

/**
 * Which arch an FDI code belongs to.
 *
 * Quadrants 1 and 2 are upper, 3 and 4 lower. The server derives the same thing from the
 * same digit and refuses a mismatch, so this is the client agreeing rather than deciding.
 */
export function jawForTooth(tooth) {
    return ['1', '2'].includes(String(tooth)[0]) ? 'upper' : 'lower';
}

/** An empty document: both arches present, so no caller has to guard for a missing key. */
export function emptyDocument() {
    return { upper: {}, lower: {} };
}

/** A deep copy. Unknown keys -- `basePlane` above all -- are carried through untouched. */
export function cloneDocument(document) {
    return JSON.parse(JSON.stringify(document ?? emptyDocument()));
}

/**
 * Normalise whatever the state endpoint returned into a document this module can mutate.
 *
 * Defensive because the alternative is worse: a missing arch would surface as a
 * `TypeError` inside a click handler, several interactions after the response that caused
 * it.
 */
export function fromState(jaws) {
    const document = emptyDocument();
    for (const jaw of JAWS) {
        const teeth = jaws?.[jaw];
        if (teeth && typeof teeth === 'object') {
            for (const [tooth, entry] of Object.entries(teeth)) {
                if (entry && typeof entry === 'object') {
                    document[jaw][String(tooth)] = JSON.parse(JSON.stringify(entry));
                }
            }
        }
    }
    return document;
}

/** The body the save endpoint takes. Both arches always, because omitting one means "keep". */
export function toSaveBody(document, expectedRevision) {
    return {
        expectedRevision: expectedRevision ?? null,
        meshes: JAWS.map((jaw) => ({ jaw, landmarks: document[jaw] ?? {} })),
    };
}

/** How many landmarks a tooth carries, for the grid's per-tooth badge. */
export function countForTooth(document, tooth) {
    const entry = document?.[jawForTooth(tooth)]?.[String(tooth)];
    if (!entry) return 0;
    return LANDMARK_TYPES.reduce((count, type) => {
        const value = entry[type];
        if (isMultiPoint(type)) {
            return count + (Array.isArray(value) ? value.length : 0);
        }
        return count + (Array.isArray(value) ? 1 : 0);
    }, 0);
}

/** Every landmark in the document, flattened, in a stable order. */
export function landmarks(document) {
    const out = [];
    for (const jaw of JAWS) {
        for (const tooth of Object.keys(document?.[jaw] ?? {}).sort()) {
            const entry = document[jaw][tooth];
            for (const type of LANDMARK_TYPES) {
                const value = entry?.[type];
                if (isMultiPoint(type)) {
                    if (!Array.isArray(value)) continue;
                    value.forEach((point, index) => {
                        if (isPoint(point)) out.push({ jaw, tooth, type, index, point });
                    });
                } else if (isPoint(value)) {
                    out.push({ jaw, tooth, type, index: null, point: value });
                }
            }
        }
    }
    return out;
}

function isPoint(value) {
    return Array.isArray(value) && value.length === 3 && value.every(Number.isFinite);
}

/**
 * Whether a point may be placed, and why not if it may not.
 *
 * Returns a message rather than a boolean so the caller has something to show. The wrong
 * jaw is the interesting case: a user picks a surface, and picking the *other* arch while
 * an upper tooth is selected is a mistake worth naming rather than silently ignoring.
 *
 * @returns {string|null} null when the placement is allowed.
 */
export function refusePlacement({ tooth, type, hitJaw }) {
    if (!tooth) return 'Select an FDI tooth';
    if (!type) return 'Select a landmark type';
    if (!EDITABLE_TYPES.includes(type)) return `${type} landmarks are read-only`;
    const expected = jawForTooth(tooth);
    if (hitJaw && hitJaw !== expected) return `Select the ${hitJaw} jaw tooth`;
    return null;
}

/** A landmark's identity, for selection. Multi-point types need the index too. */
export function sameLandmark(left, right) {
    return Boolean(left) && Boolean(right)
        && left.jaw === right.jaw && String(left.tooth) === String(right.tooth)
        && left.type === right.type && (left.index ?? null) === (right.index ?? null);
}

// ---------------------------------------------------------------------------
// Mutations. Each is index-preserving, which is what the action log rests on.
// ---------------------------------------------------------------------------

function entryFor(document, jaw, tooth) {
    const teeth = (document[jaw] ??= {});
    return (teeth[String(tooth)] ??= {});
}

function dropToothIfEmpty(document, jaw, tooth) {
    const entry = document[jaw]?.[String(tooth)];
    if (entry && Object.keys(entry).length === 0) {
        delete document[jaw][String(tooth)];
    }
}

/**
 * Place a landmark, returning what was displaced so the action can invert itself.
 *
 * A single-point type overwrites; the previous value comes back so undo can restore it
 * rather than deleting a landmark the user had not touched. A multi-point type appends
 * unless an index is given, which is what makes redo land in the same slot.
 *
 * @returns {{index: number|null, replaced: number[]|null}}
 */
export function place(document, { jaw, tooth, type, index = null, point }) {
    const entry = entryFor(document, jaw, tooth);
    const value = [Number(point[0]), Number(point[1]), Number(point[2])];
    if (isMultiPoint(type)) {
        const list = Array.isArray(entry[type]) ? entry[type] : (entry[type] = []);
        const at = index === null ? list.length : index;
        list.splice(at, 0, value);
        return { index: at, replaced: null };
    }
    const replaced = isPoint(entry[type]) ? entry[type] : null;
    entry[type] = value;
    return { index: null, replaced };
}

/**
 * Remove a landmark, returning the point removed so the action can invert itself.
 *
 * @returns {number[]|null} null when there was nothing there.
 */
export function remove(document, { jaw, tooth, type, index = null }) {
    const entry = document[jaw]?.[String(tooth)];
    if (!entry) return null;
    let removed = null;
    if (isMultiPoint(type)) {
        const list = entry[type];
        if (!Array.isArray(list) || index === null || !list[index]) return null;
        [removed] = list.splice(index, 1);
        if (list.length === 0) delete entry[type];
    } else {
        if (!isPoint(entry[type])) return null;
        removed = entry[type];
        delete entry[type];
    }
    dropToothIfEmpty(document, jaw, tooth);
    return removed;
}
