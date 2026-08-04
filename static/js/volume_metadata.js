/**
 * VolumeMetadata - orientation metadata inspection for NIfTI volumes.
 *
 * Viewers must consult a file's own orientation metadata (qform/sform)
 * before rendering, so that volumes are displayed in the coordinate system
 * the metadata declares. NiiVue reorients volumes to RAS using the header;
 * when the header carries no valid qform/sform it silently assumes the
 * storage order is RAS, which can mirror the anatomy. This module parses
 * the header up front so viewers can gate on it and warn the user.
 *
 * Depends on the vendored nifti-reader.js (global `nifti`).
 */

(function(root, factory) {
    'use strict';
    var api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    root.VolumeMetadata = api;
}(typeof window !== 'undefined' ? window : globalThis, function() {
    'use strict';

    var AXIS_PAIRS = [['L', 'R'], ['P', 'A'], ['I', 'S']];
    var ORIENTATION_CHOICES = ['RAS', 'LAS', 'RPS', 'LPS', 'RAI', 'LAI', 'RPI', 'LPI'];

    function isFiniteNumber(value) {
        return typeof value === 'number' && Number.isFinite(value);
    }

    function orientationAxisSign(code) {
        if (!/^[RL][AP][SI]$/.test(code)) {
            return null;
        }
        return {
            x: code.charAt(0) === 'R' ? 1 : -1,
            y: code.charAt(1) === 'A' ? 1 : -1,
            z: code.charAt(2) === 'S' ? 1 : -1
        };
    }

    function affineDeterminant3x3(affine) {
        var a = affine[0][0], b = affine[0][1], c = affine[0][2];
        var d = affine[1][0], e = affine[1][1], f = affine[1][2];
        var g = affine[2][0], h = affine[2][1], i = affine[2][2];
        return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g);
    }

    function isAffineValid(affine) {
        if (!affine || affine.length < 4) {
            return false;
        }
        for (var row = 0; row < 3; row += 1) {
            var line = affine[row];
            if (!line || line.length < 4) {
                return false;
            }
            for (var col = 0; col < 4; col += 1) {
                if (!isFiniteNumber(line[col])) {
                    return false;
                }
            }
        }
        var determinant = affineDeterminant3x3(affine);
        if (!isFiniteNumber(determinant) || Math.abs(determinant) < 1e-9) {
            return false;
        }
        return true;
    }

    /**
     * Derive the nibabel-style axcodes string (e.g. "RAS") from an affine.
     * Each column of the affine maps one storage axis into physical space;
     * the dominant component decides the anatomical axis, its sign the
     * direction. Returns null when the mapping is degenerate.
     */
    function affineToOrientation(affine) {
        if (!isAffineValid(affine)) {
            return null;
        }
        var codes = [];
        for (var col = 0; col < 3; col += 1) {
            var values = [affine[0][col], affine[1][col], affine[2][col]];
            var best = 0;
            for (var row = 1; row < 3; row += 1) {
                if (Math.abs(values[row]) > Math.abs(values[best])) {
                    best = row;
                }
            }
            if (values[best] === 0) {
                return null;
            }
            codes.push(values[best] > 0 ? AXIS_PAIRS[best][1] : AXIS_PAIRS[best][0]);
        }
        return codes.join('');
    }

    /**
     * Parse the NIfTI header of an ArrayBuffer and report its orientation
     * metadata. The buffer is not modified.
     *
     * @param {ArrayBuffer} arrayBuffer
     * @returns {{
     *   ok: boolean, error: (string|null),
     *   hasMetadata: boolean, orientation: (string|null),
     *   qformCode: number, sformCode: number,
     *   affine: (Array|null), dims: (Array|null), pixDims: (Array|null),
     *   issues: string[]
     * }}
     */
    function parseNiftiMetadata(arrayBuffer) {
        var result = {
            ok: false,
            error: null,
            hasMetadata: false,
            orientation: null,
            qformCode: 0,
            sformCode: 0,
            affine: null,
            dims: null,
            pixDims: null,
            issues: []
        };

        if (!arrayBuffer || typeof arrayBuffer.byteLength !== 'number') {
            result.error = 'empty-buffer';
            return result;
        }

        var niftiLib = (typeof window !== 'undefined' && window.nifti) || null;
        if (!niftiLib) {
            result.error = 'nifti-reader-unavailable';
            return result;
        }

        var header;
        try {
            header = niftiLib.readHeader(arrayBuffer);
        } catch (parseError) {
            result.error = 'parse-failed';
            result.issues.push('The file could not be parsed as NIfTI.');
            return result;
        }

        result.ok = true;
        result.qformCode = header.qform_code | 0;
        result.sformCode = header.sform_code | 0;
        result.affine = header.affine || null;
        result.dims = header.dims || null;
        result.pixDims = header.pixDims || null;

        var declaresOrientation = result.qformCode >= 1 || result.sformCode >= 1;
        if (!declaresOrientation) {
            result.issues.push(
                'qform/sform codes are 0: the file declares no orientation metadata.'
            );
        }

        var affineValid = isAffineValid(result.affine);
        if (declaresOrientation && !affineValid) {
            result.issues.push('The declared affine matrix is degenerate or unreadable.');
        }

        if (declaresOrientation && affineValid) {
            result.orientation = affineToOrientation(result.affine);
            result.hasMetadata = result.orientation !== null;
            if (!result.hasMetadata) {
                result.issues.push('The affine could not be mapped to anatomical axes.');
            }
        }

        return result;
    }

    /**
     * Build the canonical diagonal affine for an orientation code, using the
     * given voxel sizes and a zero origin. Matches the convention used by
     * nibabel's axcodes (identity affine -> "RAS").
     */
    function orientationToAffine(code, pixDims) {
        var signs = orientationAxisSign(code);
        if (!signs) {
            return null;
        }
        var spacing = [1, 1, 1];
        for (var index = 0; index < 3; index += 1) {
            var value = pixDims ? Number(pixDims[index + 1]) : NaN;
            if (isFiniteNumber(value) && value > 0) {
                spacing[index] = value;
            }
        }
        return [
            [signs.x * spacing[0], 0, 0, 0],
            [0, signs.y * spacing[1], 0, 0],
            [0, 0, signs.z * spacing[2], 0],
            [0, 0, 0, 1]
        ];
    }

    return {
        AXIS_PAIRS: AXIS_PAIRS,
        ORIENTATION_CHOICES: ORIENTATION_CHOICES,
        isAffineValid: isAffineValid,
        affineToOrientation: affineToOrientation,
        parseNiftiMetadata: parseNiftiMetadata,
        orientationToAffine: orientationToAffine,
        orientationAxisSign: orientationAxisSign
    };
}));
