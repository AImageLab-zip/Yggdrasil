'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadVolumeMetadata() {
    const window = {};
    const context = {
        window,
        console,
        TextEncoder,
        TextDecoder
    };
    vm.createContext(context);
    vm.runInNewContext(
        fs.readFileSync(path.join(__dirname, '../nifti-reader.js'), 'utf8'),
        context
    );
    vm.runInNewContext(
        fs.readFileSync(path.join(__dirname, '../volume_metadata.js'), 'utf8'),
        context
    );
    return { VolumeMetadata: window.VolumeMetadata, nifti: window.nifti, window };
}

function makeNiftiBuffer(niftiLib, { affine, qformCode = 0, sformCode = 0, pixDims = [1, 1, 1, 1] }) {
    const header = new niftiLib.NIFTI1();
    header.littleEndian = true;
    header.dims = [3, 4, 5, 6, 1, 1, 1, 1];
    header.pixDims = pixDims;
    header.datatypeCode = niftiLib.NIFTI1.TYPE_INT16;
    header.numBitsPerVoxel = 16;
    header.vox_offset = 352;
    header.qform_code = qformCode;
    header.sform_code = sformCode;
    header.affine = affine;
    header.magic = 'n+1';
    return header.toArrayBuffer();
}

test('volume with valid sform reports orientation from metadata', () => {
    const { VolumeMetadata, nifti } = loadVolumeMetadata();
    const affine = [
        [1, 0, 0, 0],
        [0, 1, 0, 0],
        [0, 0, 1, 0],
        [0, 0, 0, 1]
    ];
    const buffer = makeNiftiBuffer(nifti, { affine, sformCode: 2 });

    const result = VolumeMetadata.parseNiftiMetadata(buffer);
    assert.equal(result.ok, true);
    assert.equal(result.hasMetadata, true);
    assert.equal(result.orientation, 'RAS');
    assert.equal(result.issues.length, 0);
});

test('negative X axis affine resolves to LAS', () => {
    const { VolumeMetadata, nifti } = loadVolumeMetadata();
    const affine = [
        [-1, 0, 0, 0],
        [0, 1, 0, 0],
        [0, 0, 1, 0],
        [0, 0, 0, 1]
    ];
    const buffer = makeNiftiBuffer(nifti, { affine, sformCode: 1 });

    const result = VolumeMetadata.parseNiftiMetadata(buffer);
    assert.equal(result.ok, true);
    assert.equal(result.hasMetadata, true);
    assert.equal(result.orientation, 'LAS');
});

test('zero qform/sform codes flag missing orientation metadata', () => {
    const { VolumeMetadata, nifti } = loadVolumeMetadata();
    const affine = [
        [1, 0, 0, 0],
        [0, 1, 0, 0],
        [0, 0, 1, 0],
        [0, 0, 0, 1]
    ];
    const buffer = makeNiftiBuffer(nifti, { affine, qformCode: 0, sformCode: 0 });

    const result = VolumeMetadata.parseNiftiMetadata(buffer);
    assert.equal(result.ok, true);
    assert.equal(result.hasMetadata, false);
    assert.equal(result.orientation, null);
    assert.ok(result.issues.length > 0);
});

test('qform-only header is accepted as having metadata', () => {
    const { VolumeMetadata, nifti } = loadVolumeMetadata();
    const header = new nifti.NIFTI1();
    header.littleEndian = true;
    header.dims = [3, 2, 2, 2, 1, 1, 1, 1];
    header.pixDims = [1, 1, 1, 1];
    header.datatypeCode = nifti.NIFTI1.TYPE_INT16;
    header.numBitsPerVoxel = 16;
    header.vox_offset = 352;
    header.qform_code = 1;
    header.sform_code = 0;
    header.quatern_b = 0;
    header.quatern_c = 0;
    header.quatern_d = 0;
    header.qoffset_x = 0;
    header.qoffset_y = 0;
    header.qoffset_z = 0;
    header.magic = 'n+1';
    const buffer = header.toArrayBuffer();

    const result = VolumeMetadata.parseNiftiMetadata(buffer);
    assert.equal(result.ok, true);
    assert.equal(result.hasMetadata, true);
    assert.equal(result.orientation, 'RAS');
});

test('non-NIfTI payload is reported as a parse failure', () => {
    const { VolumeMetadata } = loadVolumeMetadata();
    const junk = new Uint8Array([0, 1, 2, 3, 4, 5]).buffer;

    const result = VolumeMetadata.parseNiftiMetadata(junk);
    assert.equal(result.ok, false);
    assert.equal(result.hasMetadata, false);
    assert.ok(result.error);
});

test('affineToOrientation matches nibabel-style axcodes', () => {
    const { VolumeMetadata } = loadVolumeMetadata();
    assert.equal(
        VolumeMetadata.affineToOrientation([
            [1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]
        ]),
        'RAS'
    );
    assert.equal(
        VolumeMetadata.affineToOrientation([
            [-1, 0, 0, 0], [0, -1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]
        ]),
        'LPS'
    );
    assert.equal(
        VolumeMetadata.affineToOrientation([
            [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 1]
        ]),
        null
    );
});

test('orientationToAffine is the inverse of affineToOrientation', () => {
    const { VolumeMetadata } = loadVolumeMetadata();
    const code = 'LAI';
    const affine = VolumeMetadata.orientationToAffine(code, [1, 0.5, 0.5, 0.5]);
    assert.equal(VolumeMetadata.affineToOrientation(affine), code);
    assert.equal(affine[0][0], -0.5);
    assert.equal(affine[1][1], 0.5);
    assert.equal(affine[2][2], -0.5);
});

test('invalid orientation codes are rejected', () => {
    const { VolumeMetadata } = loadVolumeMetadata();
    assert.equal(VolumeMetadata.orientationToAffine('RAX', [1, 1, 1, 1]), null);
    assert.equal(VolumeMetadata.orientationToAffine('RASL', [1, 1, 1, 1]), null);
    assert.equal(VolumeMetadata.orientationAxisSign('ras'), null);
});
