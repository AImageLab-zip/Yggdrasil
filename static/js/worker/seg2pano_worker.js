/* global self, importScripts */
'use strict';

self.window = self;
importScripts('/static/js/nifti-reader.js', '/static/js/seg2pano_core.js');

var nifti = self.nifti;
var core = self.Seg2PanoCore;
var segmentation = null;
var dimensions = null;
var flipZ = false;
var autoZ = null;
var segmentationMeta = null;

function typedImage(header, imageBuffer) {
    var code = header.datatypeCode;
    var little = header.littleEndian !== false;
    var bytes = header.numBitsPerVoxel / 8;
    var count = header.dims[1] * header.dims[2] * header.dims[3];
    var view;

    if (little) {
        if (code === 2) return new Uint8Array(imageBuffer, 0, count);
        if (code === 256) return new Int8Array(imageBuffer, 0, count);
        if (code === 4) return new Int16Array(imageBuffer, 0, count);
        if (code === 512) return new Uint16Array(imageBuffer, 0, count);
        if (code === 8) return new Int32Array(imageBuffer, 0, count);
        if (code === 768) return new Uint32Array(imageBuffer, 0, count);
        if (code === 16) return new Float32Array(imageBuffer, 0, count);
        if (code === 64) return new Float64Array(imageBuffer, 0, count);
    }

    if (![1, 2, 4, 8].includes(bytes)) throw new Error('Unsupported segmentation datatype: ' + code);
    view = new DataView(imageBuffer);
    var output = new Float64Array(count);
    for (var i = 0; i < count; i++) {
        var offset = i * bytes;
        if (code === 2) output[i] = view.getUint8(offset);
        else if (code === 256) output[i] = view.getInt8(offset);
        else if (code === 4) output[i] = view.getInt16(offset, little);
        else if (code === 512) output[i] = view.getUint16(offset, little);
        else if (code === 8) output[i] = view.getInt32(offset, little);
        else if (code === 768) output[i] = view.getUint32(offset, little);
        else if (code === 16) output[i] = view.getFloat32(offset, little);
        else if (code === 64) output[i] = view.getFloat64(offset, little);
        else throw new Error('Unsupported segmentation datatype: ' + code);
    }
    return output;
}

function parseSegmentation(buffer) {
    self.postMessage({ type: 'progress', stage: 'segmentation', value: 0.05 });
    var source = nifti.isCompressed(buffer) ? nifti.decompress(buffer) : buffer;
    var header = nifti.readHeader(source);
    if (!header) throw new Error('Unable to read the segmentation NIfTI header.');
    var image = nifti.readImage(header, source);
    if (!image) throw new Error('Unable to read segmentation voxels.');
    var data = typedImage(header, image);
    var slope = header.scl_slope || 1;
    var intercept = header.scl_inter || 0;
    if (slope !== 1 || intercept !== 0) {
        var scaled = new Float64Array(data.length);
        for (var i = 0; i < data.length; i++) scaled[i] = data[i] * slope + intercept;
        data = scaled;
    }
    return {
        data: data,
        dimensions: { width: header.dims[1], height: header.dims[2], depth: header.dims[3] },
        affine: header.affine || null,
        datatype: header.datatypeCode
    };
}

function sameDimensions(left, right) {
    return left.width === right.width && left.height === right.height && left.depth === right.depth;
}

function normalizedAffine(affine) {
    if (!affine) return null;
    if (Array.isArray(affine[0])) return affine;
    if (affine.length >= 16) {
        return [
            Array.prototype.slice.call(affine, 0, 4),
            Array.prototype.slice.call(affine, 4, 8),
            Array.prototype.slice.call(affine, 8, 12),
            Array.prototype.slice.call(affine, 12, 16)
        ];
    }
    return null;
}

function sameAffine(left, right) {
    left = normalizedAffine(left);
    right = normalizedAffine(right);
    if (!left || !right) return true;
    for (var row = 0; row < 4; row++) {
        for (var column = 0; column < 4; column++) {
            var a = Number(left[row][column]);
            var b = Number(right[row][column]);
            var tolerance = 1e-4 * Math.max(1, Math.abs(a), Math.abs(b));
            if (!Number.isFinite(a) || !Number.isFinite(b) || Math.abs(a - b) > tolerance) return false;
        }
    }
    return true;
}

function transferableGeometry(id, z, geometry, mask) {
    var payload = {
        type: 'geometry',
        id: id,
        z: z,
        autoZ: autoZ,
        flipZ: flipZ,
        source: geometry.source,
        polynomial: geometry.polynomial,
        start: geometry.start,
        end: geometry.end,
        controlPoints: geometry.controlPoints,
        spline: geometry.spline,
        centerline: geometry.centerline,
        slab: geometry.slab,
        mask: mask
    };
    self.postMessage(payload, [mask.buffer]);
}

function buildGeometry(id, z, controlPoints) {
    if (!segmentation) throw new Error('Segmentation has not been initialized.');
    var selectedZ = Math.max(0, Math.min(dimensions.depth - 1, Math.trunc(z)));
    self.postMessage({ type: 'progress', id: id, stage: 'geometry', value: 0.25 });
    var mask = core.mandibleMask(segmentation, dimensions, selectedZ, flipZ);
    var geometry = controlPoints && controlPoints.length
        ? core.buildEditedGeometry(controlPoints)
        : core.buildAutoGeometry(mask, dimensions.width, dimensions.height);
    transferableGeometry(id, selectedZ, geometry, mask);
}

self.onmessage = function(event) {
    var message = event.data || {};
    try {
        if (message.type === 'init') {
            var parsed = parseSegmentation(message.buffer);
            dimensions = parsed.dimensions;
            if (!message.raw || !sameDimensions(dimensions, message.raw.dimensions)) {
                throw new Error(
                    'Raw and segmentation native dimensions differ (' +
                    (message.raw ? [message.raw.dimensions.width, message.raw.dimensions.height, message.raw.dimensions.depth].join('x') : 'raw unavailable') +
                    ' vs ' + [dimensions.width, dimensions.height, dimensions.depth].join('x') + ').'
                );
            }
            if (!sameAffine(parsed.affine, message.raw.affine)) {
                throw new Error('Raw and segmentation NIfTI grids are not aligned.');
            }
            segmentation = parsed.data;
            segmentationMeta = { affine: parsed.affine, datatype: parsed.datatype };
            flipZ = typeof message.raw.flipZ === 'boolean'
                ? message.raw.flipZ
                : core.rawDerivedFlipZ(message.raw.affine);
            autoZ = core.autoSelectZ(segmentation, dimensions, flipZ);
            self.postMessage({
                type: 'initialized',
                id: message.id,
                dimensions: dimensions,
                autoZ: autoZ,
                flipZ: flipZ,
                segmentation: segmentationMeta
            });
            buildGeometry(message.id, autoZ, null);
        } else if (message.type === 'geometry') {
            buildGeometry(message.id, message.z, message.controlPoints || null);
        }
    } catch (error) {
        self.postMessage({ type: 'error', id: message.id, message: error.message || String(error) });
    }
};
