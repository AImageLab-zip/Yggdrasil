/**
 * The only part of the harness that touches a real viewer.
 *
 * Everything else in `imaging/validation/` is pure and covered by `node --test`. These
 * two functions are not, and cannot be: they read a loaded Cornerstone volume and a
 * loaded NiiVue image, which need WebGL and a document. So they are kept as thin as
 * they can be -- read fields, translate index spaces, hand back a plain object -- and
 * every decision with any judgement in it lives in the pure modules they call.
 *
 * That split is deliberate. If a leg fails in the browser, the question "is the
 * comparison logic wrong or is the viewer wrong?" has already been answered: the
 * comparison logic has 39 tests behind it and these adapters have almost no logic to
 * be wrong about.
 *
 * Both legs must return world positions in **RAS**, because that is the frame the
 * reference leg (the file's own affine) speaks. Cornerstone speaks LPS and NiiVue
 * speaks RAS, so exactly one of these converts -- and which one is the single most
 * likely place for a mirroring bug to enter, which is why it is one line, named, and
 * commented rather than folded into an expression.
 */

import { describeGeometry } from '../geometry/orientation.js';
import { fileVoxelToRasVoxel, rasDimensions, voxelToFraction } from './voxelSampling.js';

/**
 * Build the Cornerstone leg from a loaded `ImageVolume`.
 *
 * @param {object} options
 * @param {object} options.volume a Cornerstone `ImageVolume`.
 * @param {string} [options.name]
 * @returns {object} a leg for `runTier1`.
 */
export function cornerstoneLeg({ volume, name = 'cornerstone' }) {
    const { imageData, origin, direction, spacing, dimensions } = volume;

    if (!imageData || typeof imageData.indexToWorld !== 'function') {
        throw new Error(
            'The Cornerstone volume has no vtkImageData; it has not finished loading.'
        );
    }

    return {
        name,
        dimensions: Array.from(dimensions),
        spacing: Array.from(spacing),
        direction: Array.from(direction),
        indexToWorldRas(ijk) {
            // Cornerstone indexes the volume in the file's own storage order, so `ijk`
            // needs no permutation here -- unlike the NiiVue leg below.
            const lps = imageData.indexToWorld([ijk[0], ijk[1], ijk[2]]);
            return lpsToRas(lps);
        },
        /** Origin in RAS, for reporting alongside the reference. */
        originRas: lpsToRas(origin),
    };
}

/**
 * Build the NiiVue leg from a loaded `NVImage`.
 *
 * Two translations happen here and both are load-bearing:
 *
 *   1. **Index space.** NiiVue reorients every volume to RAS on load, so its indices
 *      are not the file's. `fileVoxelToRasVoxel` undoes that using `permRAS`.
 *   2. **Fractional coordinates.** `frac2mm` takes fractions of the volume, addressing
 *      voxel *centres*. `voxelToFraction` is the conversion; getting it wrong is a
 *      uniform half-voxel offset, which is invisible on screen and 1000x Tier 1's
 *      tolerance.
 *
 * `isForceSliceMM` is passed **true** on purpose. Left false, `frac2mm` uses
 * `frac2mmOrtho` -- the orthogonalised matrix NiiVue uses for its slice views, which
 * is not the volume's true world mapping for an oblique study, and comparing against
 * it would report a geometry error that is really a choice of matrix.
 *
 * @param {object} options
 * @param {object} options.niivue the `Niivue` instance.
 * @param {object} options.image `nv.volumes[index]`, an `NVImage`.
 * @param {object} options.header the parsed NIfTI header (for the file's own dims).
 * @param {string} [options.name]
 * @returns {object} a leg for `runTier1`.
 */
export function niivueLeg({ niivue, image, header, name = 'niivue' }) {
    const permRAS = image?.permRAS;
    if (!Array.isArray(permRAS)) {
        throw new Error(
            'The NiiVue image has no permRAS; it has not finished its RAS reorientation.'
        );
    }

    const described = describeGeometry(header);
    const fileDims = described.dimensions;
    const dimsRAS = rasDimensions(fileDims, permRAS);

    // NiiVue's own report of the reoriented size. If it disagrees with ours, we have
    // misread permRAS and every index below is meaningless -- so refuse rather than
    // produce a leg that would fail Tier 1 for the wrong reason.
    const reported = image.dimsRAS ? [image.dimsRAS[1], image.dimsRAS[2], image.dimsRAS[3]] : null;
    if (reported && reported.some((size, axis) => size !== dimsRAS[axis])) {
        throw new Error(
            `permRAS ${JSON.stringify(permRAS)} implies RAS dims ${JSON.stringify(dimsRAS)}, ` +
                `but NiiVue reports ${JSON.stringify(reported)}. The index mapping is wrong.`
        );
    }

    return {
        name,
        dimensions: fileDims,
        // NiiVue reorients, so its spacing and direction describe the *reoriented*
        // volume. Reporting them against the file's reference would compare a
        // permutation with its own preimage, so the derived-geometry check is left to
        // the Cornerstone leg and this one carries the reference's own values --
        // positions are what this leg is here to corroborate.
        spacing: described.spacing,
        direction: described.direction,
        permRAS: Array.from(permRAS),
        dimsRAS,
        indexToWorldRas(ijk) {
            const rasIjk = fileVoxelToRasVoxel(ijk, { permRAS, dims: fileDims });
            const frac = voxelToFraction(rasIjk, dimsRAS);
            // `true` = use frac2mm, not frac2mmOrtho. See the doc comment.
            const mm = niivue.frac2mm(frac, 0, true);
            return [mm[0], mm[1], mm[2]];
        },
    };
}

/**
 * LPS to RAS: negate x and y.
 *
 * One line, named, and on its own, because it is the single most likely place for a
 * mirroring bug to enter the harness -- and a harness that mirrors in the same
 * direction as the bug it is looking for reports success.
 *
 * @param {ArrayLike<number>} point
 * @returns {number[]}
 */
export function lpsToRas(point) {
    return [-point[0], -point[1], point[2]];
}

/**
 * Read a Cornerstone volume's cached scalar data, for Tier 2.
 *
 * @param {object} volume a Cornerstone `ImageVolume`.
 * @returns {ArrayLike<number>}
 */
export function cachedScalarData(volume) {
    const manager = volume?.voxelManager;
    if (manager?.getCompleteScalarDataArray) {
        return manager.getCompleteScalarDataArray();
    }
    if (manager?.getScalarData) {
        return manager.getScalarData();
    }
    throw new Error('The Cornerstone volume exposes no scalar data; it has not finished loading.');
}
