/**
 * What every imaging surface agrees with `annotations/` about.
 *
 * Two surfaces now speak this protocol -- the volume grid and the photo stack -- and
 * more will. It lives here rather than in `grid/` because a module the photo stack has
 * to import from `grid/` is a module in the wrong place: the next reader would
 * reasonably conclude the stack depends on the grid, and the phase after that would
 * build on that conclusion.
 *
 * Nothing here is surface-specific. The frame a surface reports handles in, the
 * descriptor it attaches, and how it groups a save are its own; the tool list, the cap
 * and the response contract are shared, and a second copy of any of them would be a
 * second thing to keep in step with the Python.
 */

/** Mirrors `MAX_ANNOTATIONS_PER_REVISION` in `annotations/services/viewer.py`. */
export const MAX_ANNOTATIONS = 500;

/**
 * The tools whose annotations are measurements.
 *
 * Mirrors `GEOMETRIC_TOOLS | INTENSITY_TOOLS` in
 * `annotations/adapters/cornerstone.py`, and `frontend/tests/measurements.test.js`
 * pins the two lists to each other.
 *
 * The list exists because `getAllAnnotations()` returns **everything Cornerstone is
 * holding**, including annotations tools keep for their own state.
 * `CrosshairsTool` is the one that bit: it stores an annotation whose `data.handles`
 * carries `toolCenter` and `rotationPoints` and **no `points` array**, so sending it
 * made the server refuse the whole save with "a Cornerstone annotation must carry at
 * least one handle" -- an accurate message about an annotation the user never drew.
 */
export const MEASUREMENT_TOOLS = Object.freeze([
    'Length',
    'Height',
    'Angle',
    'CobbAngle',
    'Bidirectional',
    'RectangleROI',
    'EllipticalROI',
    'CircleROI',
    'Probe',
]);

/**
 * Keep only the annotations that are measurements.
 *
 * @param {object[]} annotations everything `getAllAnnotations()` returned.
 * @returns {object[]}
 */
export function measurementAnnotations(annotations) {
    if (!Array.isArray(annotations)) {
        return [];
    }
    return annotations.filter((entry) => MEASUREMENT_TOOLS.includes(entry?.metadata?.toolName));
}

/**
 * Refuse a save the server would refuse, before sending it.
 *
 * @param {object[]} annotations
 * @param {number} expectedRevision
 * @throws {Error} with the reason, which is the message the caller shows.
 */
export function assertSavable(annotations, expectedRevision) {
    if (!Array.isArray(annotations)) {
        throw new Error('annotations must be an array.');
    }
    if (annotations.length > MAX_ANNOTATIONS) {
        throw new Error(
            `${annotations.length} annotations exceeds the ${MAX_ANNOTATIONS} the server ` +
                'accepts in one revision. Refusing here rather than sending a request that ' +
                'will be rejected whole.'
        );
    }
    if (!Number.isInteger(expectedRevision) || expectedRevision < 0) {
        // Not defaulted to 0: guessing means the second editor on a study loses a 409
        // they could have avoided by reading the state endpoint first.
        throw new Error(
            'expectedRevision is required. Read it from the measurements state endpoint ' +
                'rather than assuming 0, or a concurrent editor loses an avoidable conflict.'
        );
    }
}

/**
 * Interpret a save response, including the one outcome a client must not retry.
 *
 * A 409 means somebody else saved while this session was editing. Retrying with a
 * bumped revision would overwrite their work, which is exactly what the unique
 * constraint exists to prevent -- so this returns `reload: true` and the caller has to
 * re-read and reapply.
 *
 * @param {{ok: boolean, status: number}} response
 * @param {object} body the parsed JSON body.
 * @returns {{saved: boolean, reload: boolean, revision: number|null, message: string|null}}
 */
export function interpretSaveResponse(response, body) {
    if (response.ok) {
        return { saved: true, reload: false, revision: body.revision, message: null };
    }
    if (response.status === 409) {
        return {
            saved: false,
            reload: true,
            revision: null,
            message:
                'Someone else saved measurements on this study while you were editing. ' +
                'Your work has not been overwritten and neither has theirs -- reload to ' +
                'see their changes, then reapply yours.',
        };
    }
    return {
        saved: false,
        reload: false,
        revision: null,
        message: body?.error || `The save failed (HTTP ${response.status}).`,
    };
}
