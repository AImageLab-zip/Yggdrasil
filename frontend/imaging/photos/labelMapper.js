/**
 * FDI tooth code to segment index, and back.
 *
 * The roadmap calls this table load-bearing and demands it be exhaustively unit-tested,
 * and the reason is concrete: the integer is what a labelmap's voxels hold and what a
 * DICOM SEG's segment numbers are, so a wrong entry does not throw -- it relabels a
 * tooth, in an export, silently.
 *
 * ## The table is a projection, not a constant
 *
 * `annotations/migrations/0002_seed_fdi_schema.py` already seeds 32 `LabelDefinition`
 * rows with `value = (quadrant - 1) * 8 + position`, and
 * `UniqueConstraint(schema, value)` is what makes "an integer 2 in an old labelmap must
 * never change meaning" a guarantee rather than an intention. The migration's own
 * docstring says a change to the numbering is a new schema *version*, never an edit.
 *
 * So this module reproduces the seed's formula rather than inventing a second numbering,
 * and {@link assertMatchesSchema} lets a caller check the projection against the rows the
 * server actually serves. A hand-maintained copy would be a second source of truth for a
 * value that is frozen in DDL.
 *
 * ## Key on the code, never on position
 *
 * Three orderings of the same 32 teeth exist in this codebase. The seed is
 * quadrant-major (11…18, 21…28, 31…38, 41…48). The old editor's `toothCodes` array is in
 * *mouth* order (18…11, 21…28, 48…41, 31…38) so the buttons read left to right as a
 * clinician sees the arch. They agree on the set and disagree on the position of most of
 * it. Anything here that indexed an array would be right for the upper right quadrant and
 * wrong for the rest -- which is exactly the kind of bug that passes a spot check.
 */

/** Quadrants, clockwise from the patient's upper right. Matches the seed. */
export const QUADRANTS = Object.freeze([1, 2, 3, 4]);

/** Tooth positions within a quadrant, from the midline outward. Matches the seed. */
export const POSITIONS = Object.freeze([1, 2, 3, 4, 5, 6, 7, 8]);

/** The schema this mapping belongs to. A different slug is a different numbering. */
export const SCHEMA_SLUG = 'fdi-permanent';
export const SCHEMA_VERSION = 1;

/**
 * Every FDI code, in the seed's own quadrant-major order.
 *
 * Not the order the toolbar draws them in -- see the header. `MOUTH_ORDER` below is for
 * that, and the two are deliberately separate values rather than one list somebody might
 * reorder for display and break for storage.
 */
export const FDI_CODES = Object.freeze(
    QUADRANTS.flatMap((quadrant) => POSITIONS.map((position) => `${quadrant}${position}`))
);

/**
 * The same codes in the order a clinician reads the arch, for the tooth grid.
 *
 * Upper right molars inward to the midline, across to upper left; then lower right
 * inward, across to lower left. Reproduces `toothCodes` in the editor being replaced.
 */
export const MOUTH_ORDER = Object.freeze([
    '18', '17', '16', '15', '14', '13', '12', '11',
    '21', '22', '23', '24', '25', '26', '27', '28',
    '48', '47', '46', '45', '44', '43', '42', '41',
    '31', '32', '33', '34', '35', '36', '37', '38',
]);

/**
 * Is this a well-formed FDI code for permanent dentition?
 *
 * Deciduous codes (quadrants 5-8) are deliberately *not* valid: the seed's docstring says
 * they would need their own schema, and inventing values for them here would freeze a
 * numbering nobody has reviewed.
 *
 * @param {string} code
 * @returns {boolean}
 */
export function isFdiCode(code) {
    return typeof code === 'string' && /^[1-4][1-8]$/.test(code);
}

/**
 * FDI code to the integer a labelmap voxel holds.
 *
 * @param {string} code e.g. `"36"`.
 * @returns {number} 1..32
 * @throws {Error} on anything else. Not a default: a segment index chosen by fallback
 *   would put a polygon under the wrong tooth in an export, and look fine doing it.
 */
export function segmentIndexFor(code) {
    if (!isFdiCode(code)) {
        throw new Error(
            `${JSON.stringify(code)} is not an FDI permanent-dentition code (quadrants ` +
                '1-4, teeth 1-8). Refusing rather than defaulting: a guessed segment index ' +
                'relabels a tooth silently.'
        );
    }
    return (Number(code[0]) - 1) * 8 + Number(code[1]);
}

/**
 * The integer back to its FDI code.
 *
 * @param {number} segmentIndex 1..32
 * @returns {string}
 * @throws {Error} outside the range.
 */
export function fdiCodeFor(segmentIndex) {
    if (!Number.isInteger(segmentIndex) || segmentIndex < 1 || segmentIndex > 32) {
        throw new Error(
            `Segment index ${JSON.stringify(segmentIndex)} is outside 1..32. Every value in ` +
                'a labelmap must name a tooth; an unmapped one is data nobody can read.'
        );
    }
    const quadrant = Math.floor((segmentIndex - 1) / 8) + 1;
    const position = ((segmentIndex - 1) % 8) + 1;
    return `${quadrant}${position}`;
}

/**
 * Check the projection against the label rows the server actually holds.
 *
 * The point of the module is that this table is frozen in DDL, so a caller that has the
 * rows should verify rather than assume -- if the two ever disagree, the disagreement is
 * about which integer a stored voxel means, and it must surface here rather than in an
 * export months later.
 *
 * @param {Array<{code: string, value: number}>} definitions as served from `LabelDefinition`.
 * @throws {Error} naming every code that disagrees, not just the first.
 */
export function assertMatchesSchema(definitions) {
    const problems = [];
    const seen = new Set();

    for (const definition of definitions) {
        seen.add(definition.code);
        if (!isFdiCode(definition.code)) {
            problems.push(`${definition.code} is not a permanent-dentition code`);
            continue;
        }
        const expected = segmentIndexFor(definition.code);
        if (definition.value !== expected) {
            problems.push(`${definition.code}: schema says ${definition.value}, this says ${expected}`);
        }
    }
    for (const code of FDI_CODES) {
        if (!seen.has(code)) {
            problems.push(`${code} is missing from the schema`);
        }
    }

    if (problems.length) {
        throw new Error(
            `The FDI mapping disagrees with schema ${SCHEMA_SLUG} v${SCHEMA_VERSION}:\n` +
                problems.join('\n')
        );
    }
}
