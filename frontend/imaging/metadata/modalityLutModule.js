/**
 * Derive `modalityLutModule` from a raw NIfTI header, and never trust the loader's.
 *
 * Finding F1 of docs/cornerstone-roadmap.md, verified against the shipped
 * `@cornerstonejs/nifti-volume-loader@5.8.2`. `helpers/modalityScaleNifti.js` normalises
 * the header fields correctly and then applies the rescale behind:
 *
 *     if (slope !== 1 && inter !== 0) {
 *         for (let i = 0; i < nVox; i++) {
 *             scalarData[i] = intensityRaw2Scaled(scalarData[i], slope, inter);
 *         }
 *     }
 *
 * The operator must be `||`. As written, the rescale is skipped whenever *either*
 * factor is already neutral:
 *
 *   - `scl_slope = 1, scl_inter = -1024` -- the ordinary uint16-plus-intercept CT/CBCT
 *     encoding -- takes the `slope !== 1` branch as false. **No rescale at all: every
 *     voxel is off by 1024 HU**, and nothing anywhere says so.
 *   - `scl_slope = 2, scl_inter = 0` takes `inter !== 0` as false. Every voxel is
 *     half its true value.
 *
 * Decision #5 (real modality-value windowing only, percent-of-range deleted) cannot be
 * built on that. So the values are derived here from the header, unconditionally, and
 * applied explicitly by the ROI/probe/statistics layer.
 *
 * Pure by design -- dict in, dict out -- so all four branches of the upstream bug are
 * covered by `node --test` and F1 cannot regress in unnoticed.
 */

/**
 * NIfTI-1/2 semantics for the scaling fields (nifti1.h, `scl_slope`/`scl_inter`):
 * `scl_slope == 0` means "no scaling is defined", *not* "multiply by zero". A NaN or
 * missing value means the same. Both map to identity.
 */
export function normalizeScaling({ scl_slope: sclSlope, scl_inter: sclInter } = {}) {
    const slopeIsUsable = Number.isFinite(sclSlope) && sclSlope !== 0;
    const interIsUsable = Number.isFinite(sclInter);
    return {
        rescaleSlope: slopeIsUsable ? sclSlope : 1,
        rescaleIntercept: interIsUsable ? sclInter : 0,
    };
}

/**
 * Build the `modalityLutModule` payload for a metadata provider.
 *
 * @param {object} header a parsed NIfTI header (`nifti-reader-js` shape).
 * @returns {{rescaleSlope: number, rescaleIntercept: number}}
 */
export function modalityLutModule(header) {
    return normalizeScaling(header ?? {});
}

/**
 * Apply the LUT to one raw stored value.
 *
 * Unconditional, which is the whole point: there is no fast path that skips the
 * multiply-add, because that is exactly the fast path that is wrong upstream.
 *
 * @param {number} raw
 * @param {{rescaleSlope: number, rescaleIntercept: number}} lut
 * @returns {number} the modality value (HU for CT/CBCT).
 */
export function applyModalityLut(raw, { rescaleSlope, rescaleIntercept }) {
    return raw * rescaleSlope + rescaleIntercept;
}

/**
 * True when the upstream `modalityScaleNifti` would silently skip this header's rescale.
 *
 * Used by the Phase 3 validation harness (Tier 2) and by the fixtures that keep F1
 * pinned. It encodes the buggy predicate deliberately: `!(slope !== 1 && inter !== 0)`.
 *
 * @param {object} header
 * @returns {boolean}
 */
export function upstreamWouldSkipRescale(header) {
    const { rescaleSlope, rescaleIntercept } = normalizeScaling(header ?? {});
    return !(rescaleSlope !== 1 && rescaleIntercept !== 0);
}

/**
 * True when skipping the rescale would actually change the voxel values -- i.e. the
 * upstream bug is not merely latent for this header but actively wrong.
 *
 * @param {object} header
 * @returns {boolean}
 */
export function upstreamIsWrongFor(header) {
    const { rescaleSlope, rescaleIntercept } = normalizeScaling(header ?? {});
    const isIdentity = rescaleSlope === 1 && rescaleIntercept === 0;
    return upstreamWouldSkipRescale(header) && !isIdentity;
}
