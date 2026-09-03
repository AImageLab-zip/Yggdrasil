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
        TextDecoder,
        // `convertFiles` spawns its worker before dispatching, so a format it will
        // refuse still needs one to exist. Never asked to do anything here.
        Worker: function () { this.terminate = () => {}; }
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

test('DICOM is not something this converter claims to handle', () => {
    // The platform stores .nii.gz only. The predicates that used to classify a DICOM
    // conversion are gone, and their absence is the contract: anything still asking
    // this module about DICOM is asking the wrong module.
    const { CBCTConvert } = loadConvertContext();
    assert.equal(CBCTConvert.isDicomFile, undefined);
    assert.equal(CBCTConvert.isDicomBuffer, undefined);
});

test('a .dcm handed to convertFiles is refused rather than converted', () => {
    const { CBCTConvert } = loadConvertContext();
    return assert.rejects(
        CBCTConvert.convertFiles([{ name: 'slice1.dcm' }]),
        /not supported here/
    );
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
