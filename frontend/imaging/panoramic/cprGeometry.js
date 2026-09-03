/**
 * From the arch the baker fitted to the centreline `vtkImageCPRMapper` reformats along.
 *
 * ## What this module is, and what it deliberately is not
 *
 * It is **arithmetic only**: arch geometry in, world-space centreline and per-point
 * orientations out. It fits no curve, samples no slab and reads no voxels -- all of that
 * is `static/js/seg2pano_core.js`, which Phase 7 leaves untouched byte for byte so the
 * strips `common/export_catalog.py` ships keep theirs (decision #8).
 *
 * That is the whole design. The live CPR and the baked PNG must be the same reformat, and
 * the only way to be sure of that is for both to consume the same `geometry` object the
 * worker produced -- its `centerline` and its `slab` -- rather than for one of them to
 * re-derive the arch from the control points.
 *
 * ## The frame, which is the part that goes silently wrong
 *
 * The arch is authored in **RAS-reoriented voxel indices**, not file order: NiiVue used to
 * reorient every volume on load and the baker has consumed RAS-ordered voxels ever since
 * (`imaging/grid/panoramicSource.js` records why). Cornerstone's `imageData` is in file
 * order and its world frame is LPS. So a centreline point is carried across by the
 * *reoriented* affine -- which maps RAS index straight to RAS world -- and then negated
 * into LPS by `indexToWorldLps`. Feeding Cornerstone the file-order indices instead would
 * put the strip somewhere plausible and wrong.
 *
 * ## How the mapper reads the orientation, verified against the shipped shader
 *
 * `Rendering/OpenGL/ImageCPRMapper.js:538-558` builds each fragment as
 *
 *     samplingDirection  = orientation . tangentDirection     // across the strip
 *     projectionDirection = orientation . bitangentDirection  // along the slab
 *     volumePos = centerlinePos + horizontalOffset * samplingDirection
 *
 * so for a panoramic:
 *
 *   - **across the strip** is the volume's +Z axis -- the strip's rows are slices, which
 *     is what makes its height `depth` and what the baker's `outputMip[z * W + column]`
 *     already means;
 *   - **along the slab** is the in-plane arch normal, which is exactly the direction
 *     `slabCoordinates` walks from its `high` line to its `low` line.
 *
 * Both are read off the geometry rather than recomputed: the slab's own first and last
 * sample *are* the two ends of the normal, so the thickness this hands the mapper is the
 * one the baker integrates over, not a number that agrees with it today.
 *
 * The orientation is supplied as a 3x3 (`getNumberOfComponents() === 9`, converted by
 * `quat.fromMat3`), column-major, columns `[T | B | N]` -- gl-matrix's own layout, and the
 * one case of the mapper's four that needs no quaternion algebra here.
 */

import { indexToWorldLps } from '../geometry/orientation.js';

/** Components per orientation tuple. 9 selects the mapper's `mat3` branch. */
export const ORIENTATION_COMPONENTS = 9;

/** The name the mapper is told to look the orientation array up under. */
export const ORIENTATION_ARRAY_NAME = 'archOrientation';

function subtract(a, b) {
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
}

function norm(v) {
    return Math.hypot(v[0], v[1], v[2]);
}

function normalize(v) {
    const length = norm(v);
    if (!(length > 0)) {
        throw new Error('A direction of zero length cannot be normalized.');
    }
    return [v[0] / length, v[1] / length, v[2] / length];
}

function cross(a, b) {
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ];
}

/**
 * The world direction one step along the volume's third (slice) axis.
 *
 * Constant over the arch -- the whole centreline lies in one axial slice -- so it is
 * computed once and reused for every point, which is also what keeps the strip's rows
 * parallel.
 *
 * @param {number[][]} rasAffine the *reoriented* 4x4 affine.
 * @returns {{direction: number[], spacing: number}}
 */
export function sliceAxis(rasAffine) {
    const origin = indexToWorldLps(rasAffine, [0, 0, 0]);
    const step = subtract(indexToWorldLps(rasAffine, [0, 0, 1]), origin);
    return { direction: normalize(step), spacing: norm(step) };
}

/**
 * Build everything `vtkImageCPRMapper` needs, for one arch on one slice.
 *
 * @param {object} options
 * @param {object} options.geometry the worker's reply: `{centerline, slab}`.
 * @param {number} options.sliceIndex the axial index the arch was drawn on.
 * @param {number[][]} options.rasAffine the reoriented 4x4 affine.
 * @param {number[]} options.dims `[width, height, depth]` of the reoriented volume.
 * @returns {{points: Float32Array, orientations: Float32Array, lines: Uint32Array,
 *   width: number, slabThickness: number, slabSamples: number, centerPoint: number[]}}
 */
export function archCenterline({ geometry, sliceIndex, rasAffine, dims }) {
    const centerline = geometry?.centerline;
    const slab = geometry?.slab;
    if (!Array.isArray(centerline) || centerline.length < 2) {
        throw new Error('A CPR centreline needs at least two arch points.');
    }
    if (!Array.isArray(slab) || slab.length !== centerline.length) {
        throw new Error('The slab must carry one sample column per centreline point.');
    }

    const count = centerline.length;
    const points = new Float32Array(count * 3);
    const orientations = new Float32Array(count * ORIENTATION_COMPONENTS);
    const across = sliceAxis(rasAffine).direction;
    const thicknesses = [];

    for (let index = 0; index < count; index += 1) {
        const [x, y] = centerline[index];
        const world = indexToWorldLps(rasAffine, [x, y, sliceIndex]);
        points.set(world, index * 3);

        const column = slab[index];
        if (!Array.isArray(column) || column.length < 2) {
            throw new Error(`Slab column ${index} carries no samples to take a normal from.`);
        }
        const high = indexToWorldLps(rasAffine, [column[0][0], column[0][1], sliceIndex]);
        const low = indexToWorldLps(
            rasAffine, [column[column.length - 1][0], column[column.length - 1][1], sliceIndex]
        );
        const span = subtract(low, high);
        thicknesses.push(norm(span));

        // Columns of the rotation, in gl-matrix's column-major order: the direction the
        // strip is sampled across, the direction the slab is integrated along, and the
        // third that makes the triad right-handed (which is the arch's own tangent).
        const alongSlab = normalize(span);
        const remaining = cross(across, alongSlab);
        orientations.set([...across, ...alongSlab, ...normalize(remaining)],
            index * ORIENTATION_COMPONENTS);
    }

    return {
        points,
        orientations,
        lines: polyline(count),
        // The strip spans the whole volume in Z. `centerPoint` is what shifts the sampling
        // off the centreline -- which sits at one slice -- so the rows cover every slice.
        width: dims[2] * sliceAxis(rasAffine).spacing,
        centerPoint: indexToWorldLps(
            rasAffine, [(dims[0] - 1) / 2, (dims[1] - 1) / 2, (dims[2] - 1) / 2]
        ),
        // The mapper takes one scalar, and the arch's columns differ only by rounding in
        // an isotropic in-plane grid. The median is used rather than the mean so a single
        // degenerate column at the end of the arch cannot drag it.
        slabThickness: median(thicknesses),
        slabSamples: slab[0].length,
    };
}

/** vtk's cell array for one open polyline through `count` points. */
export function polyline(count) {
    const cells = new Uint32Array(count + 1);
    cells[0] = count;
    for (let index = 0; index < count; index += 1) {
        cells[index + 1] = index;
    }
    return cells;
}

function median(values) {
    const sorted = [...values].sort((a, b) => a - b);
    const middle = sorted.length >> 1;
    return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

/**
 * Which way up the strip's cross-section runs, so the live view matches the baked one.
 *
 * The baker writes output row `z` from native slice `canonicalZToNative(z, depth, flipZ)`
 * (`seg2pano_core.js:28-30`), so on a volume the predicate calls flipped its first row is
 * the *last* slice. The CPR always samples along +Z, so the two agree only if the camera
 * is stood on its head for exactly those volumes.
 *
 * Expressed here, as a number, rather than inside the viewport: it is the one place the
 * live preview and the artifact can disagree, and a disagreement of this kind reads as
 * "the panoramic is upside down" long after anybody remembers why.
 *
 * **The sign convention is a browser-check item.** The algebra fixes that the two must
 * differ by this predicate; which of the two is "up" is a fact about how the baked strip
 * has always been displayed, and only a real study settles it. If the live preview comes
 * back mirrored against the saved one, this is the single line to invert.
 *
 * @param {boolean} flipZ the descriptor's inherited predicate.
 * @returns {number} +1 to view +Z as up, -1 to invert.
 */
export function viewUpSign(flipZ) {
    return flipZ ? -1 : 1;
}
