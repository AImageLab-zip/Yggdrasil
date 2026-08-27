/**
 * Building the measurement save request, and the grid facts that travel with it.
 *
 * Pure: it takes a header, a geometry description and the viewer's annotation list,
 * and returns a plain object. The `fetch` is one line at the call site. That split is
 * not ceremony -- the interesting failures here are all in *what is sent*, and none of
 * them are visible in a browser until months later.
 *
 * Two things are worth stating outright.
 *
 * **The client sends no measurement values.** Only the geometry. The server recomputes
 * every number from the handles (`annotations/adapters/cornerstone.py`), so a value
 * sent from here would be ignored at best and believed at worst. What the viewer knows
 * that the server does not is the *shape*; what the server knows that the viewer does
 * not is whether the shape may be trusted.
 *
 * **The volume descriptor is the cross-check's evidence, not decoration.**
 * `annotations_normalize_coordinates` records shape, spacing, affine and the
 * `scl_slope`/`scl_inter` pair on the resource so a volume that gets resampled or
 * re-oriented underneath its annotations can be *detected* rather than discovered.
 * {@link volumeDescriptor} produces the same keys from the header the viewer already
 * parsed, so a measurement saved from the grid carries that evidence from the first
 * save instead of waiting for a maintenance command to sweep it in.
 *
 * The one key the sweep writes and this does not is `dtype`. After
 * `modalityScaleNifti` the cached array's type is a function of the rescale shape
 * rather than of the file, so a viewer-reported dtype would describe Cornerstone's
 * promotion and not the stored data. Absent beats wrong: the sweep reads the bytes and
 * can fill it in later, and a wrong value would have to be un-believed first.
 */

import { describeGeometry } from '../geometry/orientation.js';
import { normalizeScaling } from '../metadata/modalityLutModule.js';
import { assertSavable } from '../annotations/protocol.js';

/** The coordinate system Cornerstone volume viewports report handles in. */
export const VIEWER_COORDINATE_SYSTEM = 'patient_lps_mm';

/**
 * The primary tool that navigates rather than measures.
 *
 * The other side of the list above: the crosshair is the one tool bound to the left
 * button that is *not* a measurement, so it is the tool the grid falls back to when
 * measuring is switched off, it is never disabled with the measurement tools, and its
 * annotation is neither saved, hidden nor cleared with theirs. Named here, beside the
 * list it is the complement of, so the toolbar and the grid cannot disagree about it.
 */
export const NAVIGATION_TOOL = 'Crosshairs';

/**
 * The grid facts a saved measurement was measured against.
 *
 * Key names match `annotations_normalize_coordinates` exactly -- `shape`,
 * `spacing_mm`, `affine`, `orientation`, `qform_code`, `sform_code`,
 * `spatial_codes_absent`, `scl_slope`, `scl_inter` -- because both writers populate the
 * same `SourceResource.descriptor` and a second spelling would make the cross-check
 * compare a field against nothing.
 *
 * @param {object} header a parsed NIfTI header.
 * @returns {object}
 */
export function volumeDescriptor(header) {
    const geometry = describeGeometry(header);
    const { rescaleSlope, rescaleIntercept } = normalizeScaling(header);

    return {
        shape: geometry.dimensions,
        spacing_mm: geometry.spacing,
        affine: geometry.affine,
        orientation: geometry.axcodes,
        qform_code: Number(header?.qform_code) | 0,
        sform_code: Number(header?.sform_code) | 0,
        // The F2 flag, spelled the way the sweep spells it. True means the affine was
        // reconstructed from pixel dimensions rather than read, so anything the
        // coordinates say about *sides* is inferred.
        spatial_codes_absent: !geometry.declared,
        scl_slope: rescaleSlope,
        scl_inter: rescaleIntercept,
        // Not written by the sweep, and deliberately added: it records which client
        // anchored these coordinates, which is the first thing anyone asks when a
        // cross-check reports drift.
        recorded_by: 'volume-grid',
    };
}

/**
 * Build the POST body for a measurement save.
 *
 * @param {object} options
 * @param {number} options.fileId the `FileRegistry` row the volume came from.
 * @param {string} [options.bundleKey] the bundle member, when the volume is one.
 * @param {object[]} options.annotations the viewer's annotation list, verbatim.
 * @param {object} options.header the parsed NIfTI header.
 * @param {number} options.expectedRevision the revision the client loaded.
 * @returns {object} ready for `JSON.stringify`.
 */
export function buildSaveRequest({ fileId, bundleKey, annotations, header, expectedRevision }) {
    if (!Number.isInteger(fileId) || fileId <= 0) {
        throw new Error(`fileId must be a positive integer, got ${JSON.stringify(fileId)}.`);
    }
    assertSavable(annotations, expectedRevision);

    return {
        fileId,
        // 'primary' is the sentinel for an ordinary single-file row; the server reads
        // an absent key the same way, so it is omitted rather than sent.
        ...(bundleKey && bundleKey !== 'primary' ? { fileKey: bundleKey } : {}),
        expectedRevision,
        coordinateSystem: VIEWER_COORDINATE_SYSTEM,
        volumeDescriptor: volumeDescriptor(header),
        annotations,
    };
}

// Re-exported for the grid's own callers rather than made them import two modules for
// one save. The definitions live in `annotations/protocol.js` because the photo stack
// speaks the same protocol, and a copy here would be a second thing to keep in step
// with `annotations/adapters/cornerstone.py`.
// `MEASUREMENT_TOOLS` in particular: `NAVIGATION_TOOL` above is defined as its
// complement, and importing the two from different modules would invite them to drift.
export {
    MAX_ANNOTATIONS,
    MEASUREMENT_TOOLS,
    interpretSaveResponse,
    measurementAnnotations,
} from '../annotations/protocol.js';
