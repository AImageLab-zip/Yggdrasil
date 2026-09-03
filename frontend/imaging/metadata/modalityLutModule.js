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

/**
 * True when the shipped loader *does* apply the rescale for this header.
 *
 * The exact complement of {@link upstreamWouldSkipRescale}, named the positive way
 * because the interesting consequence is positive: when this is true, `scalarData`
 * already holds modality values and applying the LUT a second time would double the
 * intercept. F1 is usually described as "the rescale is skipped", but the operative
 * hazard for Phase 3 is that it is skipped *sometimes* -- the cached scalar data is in
 * raw units for some volumes and modality units for others, with nothing on the volume
 * saying which. Every consumer of real values has to ask.
 *
 * @param {object} header
 * @returns {boolean}
 */
export function upstreamAppliesRescale(header) {
    return !upstreamWouldSkipRescale(header);
}

/**
 * The LUT still outstanding on `scalarData` after the loader has run.
 *
 * This is the value Phase 3 registers as `modalityLutModule` with the metadata
 * provider, and it is deliberately **not** the header's own slope/intercept: applying
 * those unconditionally would be right for the two branches upstream skips and wrong
 * for the two it does not. Identity here means "the cached data is already in modality
 * units", not "this volume has no rescale".
 *
 * @param {object} header a parsed NIfTI header.
 * @returns {{rescaleSlope: number, rescaleIntercept: number}}
 */
export function residualModalityLut(header) {
    if (upstreamAppliesRescale(header)) {
        return { rescaleSlope: 1, rescaleIntercept: 0 };
    }
    return normalizeScaling(header ?? {});
}

/**
 * Convert a modality value (HU for CT/CBCT) into the units `scalarData` is stored in.
 *
 * The inverse of {@link applyModalityLut}, and the reason it is needed: a VOI is
 * authored in real units -- a bone window is 300/1500 HU whatever the file says -- but
 * the renderer samples the cached array. Decision #5 deletes percent-of-range
 * windowing; this function is what makes an absolute preset expressible against data
 * that may still be raw.
 *
 * @param {number} modalityValue
 * @param {{rescaleSlope: number, rescaleIntercept: number}} lut
 * @returns {number}
 */
export function toStoredValue(modalityValue, { rescaleSlope, rescaleIntercept }) {
    return (modalityValue - rescaleIntercept) / rescaleSlope;
}

/**
 * Whether the loader's in-place rescale can overflow the array it writes into.
 *
 * A second-order consequence of the same code path, found while writing
 * {@link residualModalityLut}. `modalityScaleNifti` picks the output typed array from
 * the *datatype and the shape of the rescale*, then writes `raw * slope + inter` back
 * into it. For `NIFTI_TYPE_INT16` with an integral rescale the output stays an
 * `Int16Array`, so a study with `scl_slope = 2` and a raw maximum above 16383 wraps
 * silently. Unlike F1 this needs the data range to demonstrate, so it is a predicate
 * the harness evaluates per study rather than a fixture.
 *
 * @param {object} header
 * @param {{min: number, max: number}} rawRange observed range of the *stored* values.
 * @returns {boolean}
 */
export function upstreamRescaleMayOverflow(header, rawRange) {
    if (!upstreamAppliesRescale(header) || !rawRange) {
        return false;
    }
    const { rescaleSlope, rescaleIntercept } = normalizeScaling(header ?? {});
    const limits = integerOutputLimits(header?.datatypeCode, rescaleSlope, rescaleIntercept);
    if (!limits) {
        return false;
    }
    const scaled = [rawRange.min, rawRange.max].map(
        (value) => value * rescaleSlope + rescaleIntercept
    );
    return scaled.some((value) => value < limits.min || value > limits.max);
}

/**
 * The range that survives the write-back, or null when the output array is floating.
 *
 * Mirrors `modalityScaleNifti`'s own array selection rather than keying on the
 * datatype alone: the same code takes a `Float32Array` branch as soon as the rescale
 * is fractional, and the two unsigned types take it for a negative rescale too, so a
 * datatype-only table would report overflow for volumes that are in fact promoted and
 * safe. Codes are from `helpers/niftiConstants.js`; the predicates are verbatim from
 * `hasFloatRescale` / `hasNegativeRescale` at the top of that function.
 *
 * @param {number} datatypeCode
 * @param {number} slope
 * @param {number} inter
 * @returns {{min: number, max: number}|null}
 */
function integerOutputLimits(datatypeCode, slope, inter) {
    const hasFloatRescale = inter % 1 !== 0 || slope % 1 !== 0;
    const hasNegativeRescale = inter < 0 || slope < 0;

    switch (datatypeCode) {
        case 2: // NIFTI_TYPE_UINT8 -> Uint8Array
            return hasFloatRescale || hasNegativeRescale ? null : { min: 0, max: 255 };
        case 512: // NIFTI_TYPE_UINT16 -> Uint16Array
            return hasFloatRescale || hasNegativeRescale ? null : { min: 0, max: 65535 };
        case 4: // NIFTI_TYPE_INT16 -> Int16Array
            return hasFloatRescale ? null : { min: -32768, max: 32767 };
        case 256:
            // NIFTI_TYPE_INT8 -> `allocateScalarData('Int8Array')`, which allocates an
            // Int16Array (finding F16). The range is Int16's, not Int8's -- which is
            // the one place F16's over-allocation is load-bearing rather than merely
            // wasteful, so it must not be "corrected" here.
            return hasFloatRescale ? null : { min: -32768, max: 32767 };
        default:
            return null;
    }
}
