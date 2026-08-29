/**
 * Describe a DICOM series in the shape the grid already understands.
 *
 * The grid's geometry, orientation and windowing layer -- `describeGeometry`,
 * `residualModalityLut`, `openingVoi` -- is written against a parsed NIfTI header.
 * Rather than forking all of it for DICOM, this module states a series in the same
 * terms. That is not a shim: every field below is a *more* direct statement of the
 * same fact, because DICOM carries the geometry explicitly where NIfTI encodes it in
 * an affine.
 *
 * Two of the values are load-bearing and worth reading twice:
 *
 * * **`qform_code: 1`.** Finding F2 is the NIfTI hazard of an *undeclared* orientation
 *   that the reader silently fabricates from pixel dimensions, possibly mirrored. A
 *   DICOM instance always carries `ImageOrientationPatient` explicitly -- it is Type 1
 *   for every image IOD this can reach -- so the orientation is declared by
 *   construction and the F2 warning correctly never fires. If it is ever *absent*, the
 *   series is malformed and {@link dicomSeriesHeader} refuses rather than inventing an
 *   affine, because a fabricated orientation is exactly what F2 is about.
 *
 * * **`scl_slope: 1, scl_inter: 0`.** Read from the shipped loader, not assumed:
 *   `imageLoader/createImage.js` defaults `preScale.enabled` to `true`, and
 *   `getScalingParameters` feeds it `RescaleSlope`/`RescaleIntercept` from the
 *   `modalityLutModule` the wadors provider populates. So a DICOM volume's cached
 *   scalar data is **already in modality units**, unconditionally -- there is no
 *   F1-style "sometimes applied" ambiguity here. Identity is precisely what
 *   `residualModalityLut` documents that state to mean, so the VOI layer is correct
 *   for DICOM with no change at all.
 *
 * Pure: no Cornerstone import, no DOM, no fetch.
 */

import { affineFromLpsGeometry } from '../geometry/orientation.js';
import { firstValue } from '../ids/dicomImageIds.js';

/** DICOM JSON tags this module reads. */
export const TAG = Object.freeze({
    MODALITY: '00080060',
    IMAGE_POSITION_PATIENT: '00200032',
    IMAGE_ORIENTATION_PATIENT: '00200037',
    ROWS: '00280010',
    COLUMNS: '00280011',
    PIXEL_SPACING: '00280030',
    SLICE_THICKNESS: '00180050',
    SPACING_BETWEEN_SLICES: '00180088',
    NUMBER_OF_FRAMES: '00280008',
});

/**
 * All values of a DICOM JSON element, as numbers.
 *
 * @param {object} document
 * @param {string} tag
 * @returns {number[]}
 */
export function numberValues(document, tag) {
    const element = document?.[tag];
    if (!element || !Array.isArray(element.Value)) {
        return [];
    }
    return element.Value.map(Number);
}

/**
 * The cross product of the two direction cosines: the slice normal.
 *
 * Computed rather than read, because DICOM does not carry it. Its *sign* is what makes
 * the volume right- or left-handed, so it is derived once here and never re-derived.
 *
 * @param {number[]} orientation six direction cosines, row then column.
 * @returns {number[]} three components.
 */
export function sliceNormal(orientation) {
    const [rx, ry, rz, cx, cy, cz] = orientation;
    return [ry * cz - rz * cy, rz * cx - rx * cz, rx * cy - ry * cx];
}

/**
 * Spacing between slices, measured from where the slices actually are.
 *
 * Preferred over `SliceThickness` deliberately: thickness is how much tissue each slice
 * integrates, which on an overlapping or gapped acquisition is *not* the distance
 * between slice centres. Using it as the k spacing squashes or stretches the volume
 * along z, and every measurement taken in a coronal or sagittal view is then wrong by
 * that ratio -- with nothing on screen to say so.
 *
 * Falls back to `SpacingBetweenSlices`, then `SliceThickness`, then 1, in that order:
 * each is a weaker statement of the same quantity.
 *
 * @param {object[]} ordered instance documents in slice order.
 * @returns {number}
 */
export function sliceSpacing(ordered) {
    const first = ordered[0];
    const orientation = numberValues(first, TAG.IMAGE_ORIENTATION_PATIENT);

    if (ordered.length > 1 && orientation.length === 6) {
        const normal = sliceNormal(orientation);
        const projections = ordered
            .map((document) => numberValues(document, TAG.IMAGE_POSITION_PATIENT))
            .filter((position) => position.length === 3)
            .map((position) => position.reduce((sum, v, i) => sum + v * normal[i], 0));
        if (projections.length > 1) {
            const span = Math.abs(projections[projections.length - 1] - projections[0]);
            const spacing = span / (projections.length - 1);
            if (Number.isFinite(spacing) && spacing > 0) {
                return spacing;
            }
        }
    }

    for (const tag of [TAG.SPACING_BETWEEN_SLICES, TAG.SLICE_THICKNESS]) {
        const value = Number(firstValue(ordered[0], tag));
        if (Number.isFinite(value) && value > 0) {
            return value;
        }
    }
    return 1;
}

/**
 * A NIfTI-shaped header describing the whole series.
 *
 * @param {object[]} instances DICOM JSON documents **in slice order** -- the order
 *   `dicomImageIds` produced the ids in, so the header and the frames describe the
 *   same stack.
 * @returns {object} a header `describeGeometry` and `openingVoi` accept.
 * @throws {Error} if the series does not declare its own orientation (see the note on
 *   F2 in this file's header).
 */
export function dicomSeriesHeader(instances) {
    if (!Array.isArray(instances) || instances.length === 0) {
        throw new Error('A series needs at least one instance document.');
    }
    const first = instances[0];

    const orientation = numberValues(first, TAG.IMAGE_ORIENTATION_PATIENT);
    if (orientation.length !== 6 || orientation.some((v) => !Number.isFinite(v))) {
        throw new Error(
            'This series carries no ImageOrientationPatient, so its orientation is ' +
                'unknown. Refusing rather than assuming one: an assumed orientation ' +
                'may be mirrored, which is finding F2 and is not detectable on screen.'
        );
    }
    const origin = numberValues(first, TAG.IMAGE_POSITION_PATIENT);
    if (origin.length !== 3 || origin.some((v) => !Number.isFinite(v))) {
        throw new Error('This series carries no ImagePositionPatient.');
    }

    const pixelSpacing = numberValues(first, TAG.PIXEL_SPACING);
    // DICOM orders PixelSpacing [row, column] -- i.e. [dy, dx]. The i axis runs along
    // the row *direction*, so its spacing is the column spacing. Transposing these is
    // invisible on an isotropic scan and a shear on every other one.
    const columnSpacing = Number.isFinite(pixelSpacing[1]) ? pixelSpacing[1] : 1;
    const rowSpacing = Number.isFinite(pixelSpacing[0]) ? pixelSpacing[0] : 1;
    const depthSpacing = sliceSpacing(instances);

    const columns = Number(firstValue(first, TAG.COLUMNS)) || 0;
    const rows = Number(firstValue(first, TAG.ROWS)) || 0;
    const frames = instances.reduce(
        (total, document) =>
            total + Math.max(1, Number(firstValue(document, TAG.NUMBER_OF_FRAMES)) || 1),
        0
    );

    const direction = [...orientation.slice(0, 3), ...orientation.slice(3, 6), ...sliceNormal(orientation)];

    return {
        affine: affineFromLpsGeometry({
            origin,
            direction,
            spacing: [columnSpacing, rowSpacing, depthSpacing],
        }),
        // Declared by construction; see the F2 note at the top of this file.
        qform_code: 1,
        sform_code: 1,
        dims: [3, columns, rows, frames],
        pixDims: [1, columnSpacing, rowSpacing, depthSpacing],
        // Already in modality units: preScale is on and the loader applied the
        // rescale. Identity here says exactly that.
        scl_slope: 1,
        scl_inter: 0,
        modality: String(firstValue(first, TAG.MODALITY) ?? ''),
    };
}
