'use strict';

/**
 * Worker-scope regression tests for the in-browser CBCT converter.
 *
 * nifti-reader.js ends with `window.nifti = {...}`, and Web Workers have no
 * `window`. Without the `self.window = self` shim the worker's importScripts call
 * throws `ReferenceError: window is not defined`, *no* imported script executes,
 * and the first missing global reached surfaces to the user as the misleading
 * "CBCT Conversion failed: VolumeMetadata is not defined".
 *
 * These tests run the worker in a context that has `self` but no `window`, which
 * is what a real Worker global scope looks like.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const STATIC_ROOT = path.join(__dirname, '..');

function resolveStaticUrl(url) {
    // The worker imports absolute Django STATIC_URL paths.
    const prefix = '/static/js/';
    assert.ok(url.startsWith(prefix), `unexpected importScripts url: ${url}`);
    return path.join(STATIC_ROOT, url.slice(prefix.length));
}

/**
 * Build a Worker-like global scope and run cbct_convert_worker.js inside it.
 * Returns the posted messages plus the context so tests can inspect globals.
 */
function loadWorker() {
    const posted = [];
    const context = {
        console,
        TextEncoder,
        TextDecoder,
        Uint8Array,
        Int16Array,
        Float32Array,
        DataView,
        ArrayBuffer
    };
    // A Worker global scope: `self` is the global object, and `window` is absent.
    context.self = context;
    context.globalThis = context;
    context.postMessage = (message) => posted.push(message);
    context.self.postMessage = context.postMessage;
    context.importScripts = function (...urls) {
        for (const url of urls) {
            vm.runInContext(fs.readFileSync(resolveStaticUrl(url), 'utf8'), context, {
                filename: url
            });
        }
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(STATIC_ROOT, 'worker/cbct_convert_worker.js'), 'utf8'),
        context,
        { filename: 'cbct_convert_worker.js' }
    );
    return { context, posted };
}

function makeNiftiBuffer(niftiLib, { affine, qformCode = 0, sformCode = 0 }) {
    const header = new niftiLib.NIFTI1();
    header.littleEndian = true;
    header.dims = [3, 2, 2, 2, 1, 1, 1, 1];
    header.pixDims = [1, 0.4, 0.4, 0.4, 1, 1, 1, 1];
    header.datatypeCode = niftiLib.NIFTI1.TYPE_INT16;
    header.numBitsPerVoxel = 16;
    header.vox_offset = 352;
    header.qform_code = qformCode;
    header.sform_code = sformCode;
    header.affine = affine;
    header.magic = 'n+1';
    const headerBuffer = header.toArrayBuffer();
    const voxelBytes = 2 * 2 * 2 * 2;
    const full = new Uint8Array(352 + voxelBytes);
    full.set(new Uint8Array(headerBuffer), 0);
    return full.buffer;
}

const IDENTITY_AFFINE = [
    [0.4, 0, 0, 0],
    [0, 0.4, 0, 0],
    [0, 0, 0.4, 0],
    [0, 0, 0, 1]
];

test('worker resolves its dependencies without a window global', () => {
    const { context } = loadWorker();
    assert.ok(context.window, 'the window shim must be installed');
    // `window` must alias the worker global, so `window.nifti` and the bare
    // `nifti` global are the same object.
    assert.equal(context.window.nifti, context.nifti, 'window must alias the worker global');
    assert.ok(context.nifti, 'nifti-reader.js must have executed');
    assert.ok(context.VolumeMetadata, 'volume_metadata.js must have executed');
    assert.equal(typeof context.fflate.gzipSync, 'function', 'fflate must be loaded locally');
});

test('parseNiftiMetadata sees orientation metadata inside the worker scope', () => {
    const { context } = loadWorker();
    const buffer = makeNiftiBuffer(context.nifti, { affine: IDENTITY_AFFINE, sformCode: 1 });

    const result = context.VolumeMetadata.parseNiftiMetadata(buffer);
    assert.equal(result.error, null, 'the reader must be reachable without `window`');
    assert.equal(result.ok, true);
    assert.equal(result.hasMetadata, true);
    assert.equal(result.orientation, 'RAS');
});

test('a NIfTI that already declares orientation is passed through, not re-oriented', () => {
    const { context, posted } = loadWorker();
    const buffer = makeNiftiBuffer(context.nifti, { affine: IDENTITY_AFFINE, sformCode: 1 });

    context.self.onmessage({ data: { type: 'PROCESS_NIFTI', buffer } });

    assert.equal(posted.length, 1);
    assert.equal(posted[0].ok, true, posted[0].error);
    assert.equal(posted[0].orientation, 'RAS');
    assert.equal(posted[0].repaired, false, 'a valid affine must never be rewritten');
});

test('a NIfTI with no orientation metadata still asks for one', () => {
    const { context, posted } = loadWorker();
    const buffer = makeNiftiBuffer(context.nifti, { affine: IDENTITY_AFFINE });

    context.self.onmessage({ data: { type: 'PROCESS_NIFTI', buffer } });

    assert.equal(posted.length, 1);
    assert.equal(posted[0].ok, false);
    assert.equal(posted[0].error, 'NEEDS_ORIENTATION');
});

test('a dependency load failure reports itself instead of a bare ReferenceError', () => {
    const posted = [];
    const context = { console, TextEncoder, TextDecoder };
    context.self = context;
    context.globalThis = context;
    context.self.postMessage = (message) => posted.push(message);
    context.importScripts = function () {
        throw new Error('network unreachable');
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(STATIC_ROOT, 'worker/cbct_convert_worker.js'), 'utf8'),
        context,
        { filename: 'cbct_convert_worker.js' }
    );

    context.self.onmessage({ data: { type: 'PROCESS_NIFTI', buffer: new ArrayBuffer(8) } });

    assert.equal(posted.length, 1);
    assert.equal(posted[0].ok, false);
    assert.match(posted[0].error, /dependencies failed to load/);
    assert.match(posted[0].error, /network unreachable/);
});

// --- MetaImage (.mha) ---------------------------------------------------------

function makeMetaImage(headerLines, payload) {
    const header = new TextEncoder().encode(headerLines.join('\n') + '\n');
    const bytes = new Uint8Array(header.length + payload.byteLength);
    bytes.set(header, 0);
    bytes.set(new Uint8Array(payload.buffer, payload.byteOffset, payload.byteLength), header.length);
    return bytes.buffer;
}

const MHA_BASE = [
    'ObjectType = Image',
    'NDims = 3',
    'DimSize = 2 2 2',
    'ElementSpacing = 0.3 0.3 0.3',
    'Offset = 0 0 0',
    'TransformMatrix = 1 0 0 0 1 0 0 0 1'
];

function convertMha(context, headerLines, payload) {
    const posted = [];
    context.self.postMessage = (message) => posted.push(message);
    context.self.onmessage({
        data: { type: 'CONVERT_METAIMAGE', buffer: makeMetaImage(headerLines, payload) }
    });
    return posted[0];
}

test('MetaImage voxel datatype is honoured instead of assumed Int16', () => {
    const { context } = loadWorker();
    // 40000 does not fit a signed 16-bit voxel; read as Int16 it wraps negative.
    const payload = new Uint16Array([40000, 1, 2, 3, 4, 5, 6, 7]);
    const result = convertMha(context, MHA_BASE.concat([
        'ElementType = MET_USHORT',
        'ElementDataFile = LOCAL'
    ]), payload);

    assert.equal(result.ok, true, result.error);
    const decompressed = context.nifti.decompress(result.buffer);
    const header = context.nifti.readHeader(decompressed);
    assert.equal(header.datatypeCode, context.nifti.NIFTI1.TYPE_UINT16);
    assert.equal(header.numBitsPerVoxel, 16);
    const voxels = new Uint16Array(context.nifti.readImage(header, decompressed));
    assert.equal(voxels[0], 40000, 'unsigned voxels must survive the round trip');
});

test('MetaImage float payloads convert to FLOAT32, not INT16', () => {
    const { context } = loadWorker();
    const payload = new Float32Array([1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5]);
    const result = convertMha(context, MHA_BASE.concat([
        'ElementType = MET_FLOAT',
        'ElementDataFile = LOCAL'
    ]), payload);

    assert.equal(result.ok, true, result.error);
    const decompressed = context.nifti.decompress(result.buffer);
    const header = context.nifti.readHeader(decompressed);
    assert.equal(header.datatypeCode, context.nifti.NIFTI1.TYPE_FLOAT32);
    const voxels = new Float32Array(context.nifti.readImage(header, decompressed));
    assert.equal(voxels[0], 1.5);
});

test('MetaImage with an external data file is rejected with an explanation', () => {
    const { context } = loadWorker();
    const result = convertMha(context, MHA_BASE.concat([
        'ElementType = MET_SHORT',
        'ElementDataFile = volume.raw'
    ]), new Int16Array(8));

    assert.equal(result.ok, false);
    assert.match(result.error, /separate file/);
    assert.match(result.error, /volume\.raw/);
});

test('compressed MetaImage data is rejected with an explanation', () => {
    const { context } = loadWorker();
    const result = convertMha(context, MHA_BASE.concat([
        'CompressedData = True',
        'ElementType = MET_SHORT',
        'ElementDataFile = LOCAL'
    ]), new Int16Array(8));

    assert.equal(result.ok, false);
    assert.match(result.error, /Compressed MetaImage/);
});

test('MetaImage orientation is derived from its TransformMatrix', () => {
    const { context } = loadWorker();
    const result = convertMha(context, [
        'ObjectType = Image',
        'NDims = 3',
        'DimSize = 2 2 2',
        'ElementSpacing = 0.3 0.3 0.3',
        'Offset = 0 0 0',
        'TransformMatrix = 1 0 0 0 1 0 0 0 1',
        'ElementType = MET_SHORT',
        'ElementDataFile = LOCAL'
    ], new Int16Array(8));

    assert.equal(result.ok, true, result.error);
    // An identity ITK (LPS) matrix maps to a NIfTI affine of diag(-1, -1, 1).
    assert.equal(result.orientation, 'LPS');
});

// --- DICOM is not converted here -----------------------------------------------

/**
 * The platform stores .nii.gz only and has no DICOM code left. The browser
 * conversion that used to destroy a series is deleted, and these two tests are what
 * stop it coming back: a re-added branch would be silent data loss producing a volume
 * no upload path on the server would accept.
 */
test('the worker has no DICOM conversion branch', () => {
    const { context, posted } = loadWorker();
    context.self.onmessage({ data: { type: 'CONVERT_DICOM_SERIES', buffers: [] } });
    assert.equal(posted.length, 1);
    assert.equal(posted[0].ok, false);
    assert.match(posted[0].error, /Unknown conversion request type/);
});

test('the worker source carries no DICOM parser', () => {
    const source = fs.readFileSync(
        path.join(__dirname, '../worker/cbct_convert_worker.js'), 'utf8'
    );
    for (const symbol of ['parseDicomHeader', 'convertDicomSeries', 'dicomSliceArrayType']) {
        assert.equal(source.includes(symbol), false, `${symbol} is back in the worker`);
    }
});
