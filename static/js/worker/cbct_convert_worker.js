/**
 * CBCT Convert Worker - In-browser converter for CBCT modalities.
 * Converts DICOM series, MetaImage (.mha), and raw/uncompressed NIfTI (.nii)
 * into a single compressed NIfTI (.nii.gz) with valid orientation metadata (qform/sform >= 1).
 */

/* global importScripts, self, VolumeMetadata, nifti, fflate */

if (typeof importScripts === 'function') {
    try {
        importScripts(
            '/static/js/nifti-reader.js',
            '/static/js/volume_metadata.js',
            'https://cdn.jsdelivr.net/npm/fflate@0.8.2/umd/index.min.js'
        );
    } catch (err) {
        console.error('CBCTConvertWorker: error loading dependencies:', err);
    }
}

/**
 * Scan binary DICOM slice for key header elements.
 */
function parseDicomHeader(arrayBuffer) {
    const view = new DataView(arrayBuffer);
    const bytes = new Uint8Array(arrayBuffer);
    const len = arrayBuffer.byteLength;

    if (len < 132) {
        return null;
    }

    // Check DICM magic at 128
    const isDicom = bytes[128] === 0x44 && bytes[129] === 0x49 && bytes[130] === 0x43 && bytes[131] === 0x4d;
    let offset = isDicom ? 132 : 0;

    let rows = 0;
    let cols = 0;
    let bitsAllocated = 16;
    let pixelRepresentation = 0;
    let pixelSpacing = [1.0, 1.0];
    let imagePosition = [0.0, 0.0, 0.0];
    let imageOrientation = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0];
    let rescaleIntercept = 0.0;
    let rescaleSlope = 1.0;
    let pixelDataOffset = -1;
    let pixelDataLength = 0;

    function readString(start, length) {
        let str = '';
        for (let i = 0; i < length; i++) {
            const charCode = bytes[start + i];
            if (charCode === 0) break;
            str += String.fromCharCode(charCode);
        }
        return str.trim();
    }

    function parseDS(str) {
        return str.split('\\').map(val => parseFloat(val.trim())).filter(val => !isNaN(val));
    }

    // Detect if Explicit VR
    let isExplicitVR = true;
    if (offset + 8 <= len) {
        const vr0 = bytes[offset + 4];
        const vr1 = bytes[offset + 5];
        if (vr0 < 65 || vr0 > 90 || vr1 < 65 || vr1 > 90) {
            isExplicitVR = false;
        }
    }

    while (offset + 8 <= len) {
        const group = view.getUint16(offset, true);
        const element = view.getUint16(offset + 2, true);
        offset += 4;

        let elementLength = 0;
        if (isExplicitVR) {
            const vr = String.fromCharCode(bytes[offset], bytes[offset + 1]);
            offset += 2;
            if (['OB', 'OW', 'OF', 'SQ', 'UT', 'UN'].includes(vr)) {
                offset += 2; // Reserved
                elementLength = view.getUint32(offset, true);
                offset += 4;
            } else {
                elementLength = view.getUint16(offset, true);
                offset += 2;
            }
        } else {
            elementLength = view.getUint32(offset, true);
            offset += 4;
        }

        if (elementLength === 0xFFFFFFFF) {
            // Undefined length element (e.g. sequence or compressed pixel data)
            if (group === 0x7FE0 && element === 0x0010) {
                pixelDataOffset = offset;
                break;
            }
            elementLength = 0;
        }

        if (offset + elementLength > len) {
            break;
        }

        // Tag matching
        if (group === 0x0020 && element === 0x0032) {
            imagePosition = parseDS(readString(offset, elementLength));
        } else if (group === 0x0020 && element === 0x0037) {
            imageOrientation = parseDS(readString(offset, elementLength));
        } else if (group === 0x0028 && element === 0x0010) {
            rows = view.getUint16(offset, true);
        } else if (group === 0x0028 && element === 0x0011) {
            cols = view.getUint16(offset, true);
        } else if (group === 0x0028 && element === 0x0030) {
            pixelSpacing = parseDS(readString(offset, elementLength));
        } else if (group === 0x0028 && element === 0x0100) {
            bitsAllocated = view.getUint16(offset, true);
        } else if (group === 0x0028 && element === 0x0103) {
            pixelRepresentation = view.getUint16(offset, true);
        } else if (group === 0x0028 && element === 0x1052) {
            const Intercepts = parseDS(readString(offset, elementLength));
            if (Intercepts.length) rescaleIntercept = Intercepts[0];
        } else if (group === 0x0028 && element === 0x1053) {
            const Slopes = parseDS(readString(offset, elementLength));
            if (Slopes.length) rescaleSlope = Slopes[0];
        } else if (group === 0x7FE0 && element === 0x0010) {
            pixelDataOffset = offset;
            pixelDataLength = elementLength;
            break;
        }

        offset += elementLength;
    }

    if (rows === 0 || cols === 0 || pixelDataOffset === -1) {
        return null;
    }

    if (pixelSpacing.length < 2) pixelSpacing = [1.0, 1.0];
    if (imagePosition.length < 3) imagePosition = [0.0, 0.0, 0.0];
    if (imageOrientation.length < 6) imageOrientation = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0];

    return {
        rows,
        cols,
        bitsAllocated,
        pixelRepresentation,
        pixelSpacing,
        imagePosition,
        imageOrientation,
        rescaleIntercept,
        rescaleSlope,
        pixelDataOffset,
        pixelDataLength,
        buffer: arrayBuffer
    };
}

/**
 * Convert an array of DICOM slice buffers to compressed NIfTI (.nii.gz).
 */
function convertDicomSeries(sliceBuffers) {
    const slices = [];
    for (let i = 0; i < sliceBuffers.length; i++) {
        const header = parseDicomHeader(sliceBuffers[i]);
        if (header) {
            slices.push(header);
        }
    }

    if (slices.length === 0) {
        throw new Error('No valid DICOM slices could be parsed from the uploaded files.');
    }

    const first = slices[0];
    const r = first.imageOrientation;
    const r_row = [r[0], r[1], r[2]];
    const r_col = [r[3], r[4], r[5]];

    // Compute normal vector n = r_row x r_col
    const n = [
        r_row[1] * r_col[2] - r_row[2] * r_col[1],
        r_row[2] * r_col[0] - r_row[0] * r_col[2],
        r_row[0] * r_col[1] - r_row[1] * r_col[0]
    ];

    // Sort slices along normal direction
    slices.forEach(slice => {
        const pos = slice.imagePosition;
        slice.dist = pos[0] * n[0] + pos[1] * n[1] + pos[2] * n[2];
    });

    slices.sort((a, b) => a.dist - b.dist);

    const numSlices = slices.length;
    let dz = 1.0;
    if (numSlices > 1) {
        dz = Math.abs(slices[1].dist - slices[0].dist);
        if (!dz || isNaN(dz)) dz = 1.0;
    }

    const dx = first.pixelSpacing[1]; // dy, dx in DICOM
    const dy = first.pixelSpacing[0];
    const origin = slices[0].imagePosition;

    // Convert DICOM LPS coordinates to NIfTI RAS coordinates (negate X and Y)
    const affine = [
        [-r_row[0] * dx, -r_col[0] * dy, -n[0] * dz, -origin[0]],
        [-r_row[1] * dx, -r_col[1] * dy, -n[1] * dz, -origin[1]],
        [ r_row[2] * dx,  r_col[2] * dy,  n[2] * dz,  origin[2]],
        [0, 0, 0, 1]
    ];

    const width = first.cols;
    const height = first.rows;
    const totalVoxels = width * height * numSlices;
    const imgData = new Int16Array(totalVoxels);

    const voxelsPerSlice = width * height;
    for (let sliceIdx = 0; sliceIdx < numSlices; sliceIdx++) {
        const slice = slices[sliceIdx];
        const sliceView = new Int16Array(slice.buffer, slice.pixelDataOffset, voxelsPerSlice);
        const destOffset = sliceIdx * voxelsPerSlice;
        imgData.set(sliceView.subarray(0, voxelsPerSlice), destOffset);
    }

    return createNiftiGzBuffer({
        width,
        height,
        depth: numSlices,
        dx,
        dy,
        dz,
        affine,
        imgData,
        scl_slope: first.rescaleSlope,
        scl_inter: first.rescaleIntercept
    });
}

/**
 * Convert MetaImage (.mha) buffer to compressed NIfTI (.nii.gz).
 */
function convertMetaImage(arrayBuffer) {
    const textDecoder = new TextDecoder();
    const bytes = new Uint8Array(arrayBuffer);
    let headerText = '';
    let headerLength = 0;

    for (let i = 0; i < Math.min(bytes.length, 4096); i++) {
        if (bytes[i] === 10) { // newline
            const line = textDecoder.decode(bytes.subarray(headerLength, i)).trim();
            headerText += line + '\n';
            headerLength = i + 1;
            if (line.startsWith('ElementDataFile = LOCAL') || line.startsWith('ElementDataFile=LOCAL')) {
                break;
            }
        }
    }

    const metadata = {};
    headerText.split('\n').forEach(line => {
        const parts = line.split('=');
        if (parts.length === 2) {
            metadata[parts[0].trim()] = parts[1].trim();
        }
    });

    const dimSize = (metadata['DimSize'] || '').split(/\s+/).map(Number);
    const elementSpacing = (metadata['ElementSpacing'] || '').split(/\s+/).map(Number);
    const offset = (metadata['Offset'] || '').split(/\s+/).map(Number);
    const matrixStr = metadata['TransformMatrix'] || '1 0 0 0 1 0 0 0 1';
    const matrix = matrixStr.split(/\s+/).map(Number);

    if (dimSize.length < 3) {
        throw new Error('Invalid MetaImage file: missing or incomplete DimSize.');
    }

    const width = dimSize[0];
    const height = dimSize[1];
    const depth = dimSize[2];
    const dx = elementSpacing[0] || 1.0;
    const dy = elementSpacing[1] || 1.0;
    const dz = elementSpacing[2] || 1.0;
    const ox = offset[0] || 0.0;
    const oy = offset[1] || 0.0;
    const oz = offset[2] || 0.0;

    // Convert ITK LPS matrix to NIfTI RAS affine
    const m = matrix;
    const affine = [
        [-m[0] * dx, -m[1] * dy, -m[2] * dz, -ox],
        [-m[3] * dx, -m[4] * dy, -m[5] * dz, -oy],
        [ m[6] * dx,  m[7] * dy,  m[8] * dz,  oz],
        [0, 0, 0, 1]
    ];

    const rawData = bytes.subarray(headerLength);
    const totalVoxels = width * height * depth;
    const imgData = new Int16Array(totalVoxels);

    const sourceView = new Int16Array(rawData.buffer, rawData.byteOffset, Math.min(totalVoxels, Math.floor(rawData.byteLength / 2)));
    imgData.set(sourceView);

    return createNiftiGzBuffer({
        width,
        height,
        depth,
        dx,
        dy,
        dz,
        affine,
        imgData
    });
}

/**
 * Convert or repair a NIfTI buffer.
 */
function processNiftiBuffer(arrayBuffer, requestedOrientation) {
    const meta = VolumeMetadata.parseNiftiMetadata(arrayBuffer);

    if (meta.ok && meta.hasMetadata) {
        // Valid metadata exists. If already gzip, pass-through. Otherwise compress.
        if (nifti.isCompressed(arrayBuffer)) {
            return {
                buffer: arrayBuffer,
                orientation: meta.orientation,
                repaired: false
            };
        }
        const compressed = fflate.gzipSync(new Uint8Array(arrayBuffer));
        return {
            buffer: compressed.buffer,
            orientation: meta.orientation,
            repaired: false
        };
    }

    // Missing metadata (qform/sform == 0)
    if (!requestedOrientation) {
        return {
            needsOrientation: true,
            dims: meta.dims,
            pixDims: meta.pixDims
        };
    }

    // Rewrite header with user-requested orientation
    let decompressedBuffer = arrayBuffer;
    if (nifti.isCompressed(arrayBuffer)) {
        decompressedBuffer = nifti.decompress(arrayBuffer);
    }

    const header = nifti.readHeader(decompressedBuffer);
    const newAffine = VolumeMetadata.orientationToAffine(requestedOrientation, header.pixDims);
    if (!newAffine) {
        throw new Error(`Invalid orientation code requested: '${requestedOrientation}'`);
    }

    header.qform_code = 1;
    header.sform_code = 1;
    header.affine = newAffine;

    const imgBytes = nifti.readImage(header, decompressedBuffer);

    const newNiftiHeader = new nifti.NIFTI1();
    newNiftiHeader.littleEndian = header.littleEndian;
    newNiftiHeader.dims = header.dims;
    newNiftiHeader.pixDims = header.pixDims;
    newNiftiHeader.datatypeCode = header.datatypeCode;
    newNiftiHeader.numBitsPerVoxel = header.numBitsPerVoxel;
    newNiftiHeader.vox_offset = 352;
    newNiftiHeader.scl_slope = header.scl_slope || 1.0;
    newNiftiHeader.scl_inter = header.scl_inter || 0.0;
    newNiftiHeader.qform_code = 1;
    newNiftiHeader.sform_code = 1;
    newNiftiHeader.affine = newAffine;
    newNiftiHeader.magic = 'n+1';

    const headerBuffer = newNiftiHeader.toArrayBuffer();
    const fullBuffer = new Uint8Array(352 + imgBytes.byteLength);
    fullBuffer.set(new Uint8Array(headerBuffer), 0);
    fullBuffer.set(new Uint8Array(imgBytes), 352);

    const compressed = fflate.gzipSync(fullBuffer);
    return {
        buffer: compressed.buffer,
        orientation: requestedOrientation,
        repaired: true
    };
}

/**
 * Build a compressed NIfTI-1 file (.nii.gz) ArrayBuffer.
 */
function createNiftiGzBuffer(spec) {
    const { width, height, depth, dx, dy, dz, affine, imgData, scl_slope = 1.0, scl_inter = 0.0 } = spec;

    const header = new nifti.NIFTI1();
    header.littleEndian = true;
    header.dims = [3, width, height, depth, 1, 1, 1, 1];
    header.pixDims = [1, dx, dy, dz, 1, 1, 1, 1];
    header.datatypeCode = nifti.NIFTI1.TYPE_INT16;
    header.numBitsPerVoxel = 16;
    header.vox_offset = 352;
    header.scl_slope = scl_slope;
    header.scl_inter = scl_inter;
    header.qform_code = 1;
    header.sform_code = 1;
    header.affine = affine;
    header.magic = 'n+1';

    const headerBuffer = header.toArrayBuffer();
    const rawImageBytes = new Uint8Array(imgData.buffer, imgData.byteOffset, imgData.byteLength);

    const uncompressed = new Uint8Array(352 + rawImageBytes.byteLength);
    uncompressed.set(new Uint8Array(headerBuffer), 0);
    uncompressed.set(rawImageBytes, 352);

    const compressed = fflate.gzipSync(uncompressed);
    const orientation = VolumeMetadata.affineToOrientation(affine) || 'RAS';

    return {
        buffer: compressed.buffer,
        orientation: orientation,
        repaired: true
    };
}

// Worker message listener
self.onmessage = function (event) {
    const data = event.data || {};
    const type = data.type;

    try {
        if (type === 'CONVERT_DICOM_SERIES') {
            const result = convertDicomSeries(data.buffers);
            self.postMessage({ ok: true, buffer: result.buffer, orientation: result.orientation }, [result.buffer]);
        } else if (type === 'CONVERT_METAIMAGE') {
            const result = convertMetaImage(data.buffer);
            self.postMessage({ ok: true, buffer: result.buffer, orientation: result.orientation }, [result.buffer]);
        } else if (type === 'PROCESS_NIFTI') {
            const result = processNiftiBuffer(data.buffer, data.requestedOrientation);
            if (result.needsOrientation) {
                self.postMessage({ ok: false, error: 'NEEDS_ORIENTATION', pixDims: result.pixDims, dims: result.dims });
            } else {
                self.postMessage({ ok: true, buffer: result.buffer, orientation: result.orientation, repaired: result.repaired }, [result.buffer]);
            }
        } else {
            self.postMessage({ ok: false, error: `Unknown conversion request type '${type}'` });
        }
    } catch (err) {
        self.postMessage({ ok: false, error: err.message || String(err) });
    }
};
