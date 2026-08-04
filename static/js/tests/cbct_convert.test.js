'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadConvertContext() {
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
    vm.runInNewContext(
        fs.readFileSync(path.join(__dirname, '../cbct_convert.js'), 'utf8'),
        context
    );
    return { CBCTConvert: window.CBCTConvert, VolumeMetadata: window.VolumeMetadata, nifti: window.nifti };
}

test('isDicomFile detects DICOM files and extensionless files', () => {
    const { CBCTConvert } = loadConvertContext();
    assert.equal(CBCTConvert.isDicomFile({ name: 'slice1.dcm' }), true);
    assert.equal(CBCTConvert.isDicomFile({ name: 'slice1.DICOM' }), true);
    assert.equal(CBCTConvert.isDicomFile({ name: '00000001' }), true);
    assert.equal(CBCTConvert.isDicomFile({ name: 'volume.nii.gz' }), false);
});

test('isMetaImageFile detects .mha files', () => {
    const { CBCTConvert } = loadConvertContext();
    assert.equal(CBCTConvert.isMetaImageFile({ name: 'head.mha' }), true);
    assert.equal(CBCTConvert.isMetaImageFile({ name: 'head.mhd' }), false);
});

test('isNiftiFile detects .nii and .nii.gz files', () => {
    const { CBCTConvert } = loadConvertContext();
    assert.equal(CBCTConvert.isNiftiFile({ name: 'scan.nii' }), true);
    assert.equal(CBCTConvert.isNiftiFile({ name: 'scan.nii.gz' }), true);
    assert.equal(CBCTConvert.isNiftiFile({ name: 'scan.dcm' }), false);
});
