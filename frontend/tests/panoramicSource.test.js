import test from 'node:test';
import assert from 'node:assert/strict';

import {
    VOLUME_READY_EVENT,
    announceVolumeReady,
    installPanoramicBridge,
    nativeRawVolumeDescriptor,
    panorexSegmentationSource,
} from '../imaging/grid/panoramicSource.js';
import { indexToWorldRas } from '../imaging/geometry/orientation.js';

const DIMS = [4, 5, 6];

function header(affine, extra = {}) {
    return {
        qform_code: 0,
        sform_code: 1,
        dims: [3, ...DIMS],
        pixDims: [1, 1, 1, 1],
        scl_slope: 1,
        scl_inter: 0,
        datatypeCode: 4,
        affine,
        ...extra,
    };
}

/** Voxels encoding their own file-order index, so a permutation is detectable. */
function indexVolume() {
    const data = new Int16Array(DIMS[0] * DIMS[1] * DIMS[2]);
    let cursor = 0;
    for (let z = 0; z < DIMS[2]; z += 1) {
        for (let y = 0; y < DIMS[1]; y += 1) {
            for (let x = 0; x < DIMS[0]; x += 1) {
                data[cursor] = x + y * 10 + z * 100;
                cursor += 1;
            }
        }
    }
    return data;
}

const RAS = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]];
// Input x -> RAS y, input y -> RAS z, input z -> RAS x, with x flipped.
const PERMUTED = [[0, 0, -1, 12], [1, 0, 0, -3], [0, 1, 0, 7], [0, 0, 0, 1]];

const SOURCE = { fileId: '42', fileKey: 'volume_nifti', revision: 3 };
const ORIGIN = 'https://ygg.example';

// ---------------------------------------------------------------------------
// The descriptor shape the panoramic reads
// ---------------------------------------------------------------------------

test('the descriptor carries every field the old NiiVue one did', () => {
    // niivue_viewer.js:690-731. The consumer is unchanged and reads all of them.
    const descriptor = nativeRawVolumeDescriptor({
        scalarData: indexVolume(),
        header: header(RAS),
        source: SOURCE,
        fileName: 'scan.nii.gz',
    });

    for (const field of ['data', 'dimensions', 'affine', 'flipZ', 'slope', 'intercept', 'datatype', 'fileName', 'source']) {
        assert.ok(field in descriptor, `missing ${field}`);
    }
    assert.deepEqual(descriptor.dimensions, { width: 4, height: 5, depth: 6 });
    assert.deepEqual(descriptor.source, SOURCE);
    assert.equal(descriptor.slope, 1);
    assert.equal(descriptor.intercept, 0);
    assert.equal(descriptor.datatype, 4);
});

test('the voxels are RAS-ordered, not file-ordered', () => {
    // The whole reason this module exists. NiiVue reoriented on load, so the panoramic
    // has always consumed RAS order; handing it file order would transpose every
    // exported strip while every test still passed.
    const descriptor = nativeRawVolumeDescriptor({
        scalarData: indexVolume(),
        header: header(PERMUTED),
        source: SOURCE,
    });

    // Input dims [4,5,6] with output x reading input z => [6,4,5].
    assert.deepEqual(descriptor.dimensions, { width: 6, height: 4, depth: 5 });
    assert.notDeepEqual(Array.from(descriptor.data.slice(0, 4)), [0, 1, 2, 3]);
});

test('the descriptor affine still describes the reoriented data', () => {
    // Reorientation moves voxels AND rewrites the affine; the physical location of a
    // voxel must not change. Getting the origin wrong is a whole-field translation --
    // the anatomy intact and in the wrong place.
    const descriptor = nativeRawVolumeDescriptor({
        scalarData: indexVolume(),
        header: header(PERMUTED),
        source: SOURCE,
    });

    const { width, height, depth } = descriptor.dimensions;
    for (const out of [[0, 0, 0], [2, 1, 3], [width - 1, height - 1, depth - 1]]) {
        const world = indexToWorldRas(descriptor.affine, out);
        assert.ok(world.every(Number.isFinite));
    }
    // The corner voxel of the reoriented volume is a real corner of the original.
    const corner = indexToWorldRas(descriptor.affine, [0, 0, 0]);
    assert.equal(corner.length, 3);
});

test('an already-RAS volume is passed through without a copy', () => {
    const data = indexVolume();
    const descriptor = nativeRawVolumeDescriptor({
        scalarData: data,
        header: header(RAS),
        source: SOURCE,
    });
    assert.equal(descriptor.data, data, 'a 60M-voxel copy that changes nothing');
});

test('the voxels stay raw: the panoramic applies slope and intercept itself', () => {
    // Pre-scaling here would double the rescale, because the consumer does it too.
    const data = indexVolume();
    const descriptor = nativeRawVolumeDescriptor({
        scalarData: data,
        header: header(RAS, { scl_slope: 1, scl_inter: -1024 }),
        source: SOURCE,
    });
    assert.equal(descriptor.data[5], data[5]);
    assert.equal(descriptor.intercept, -1024);
});

test('a short or absent array yields null rather than a padded panoramic', () => {
    // The same guard the NiiVue version had: a short array means the load is unfinished.
    assert.equal(
        nativeRawVolumeDescriptor({ scalarData: new Int16Array(10), header: header(RAS), source: SOURCE }),
        null
    );
    assert.equal(
        nativeRawVolumeDescriptor({ scalarData: null, header: header(RAS), source: SOURCE }),
        null
    );
});

test('a volume with an inferred orientation is flagged on the descriptor', () => {
    // F2. Not part of the old shape, and additive: a panoramic baked from a volume
    // whose left/right was guessed can be told apart later.
    const descriptor = nativeRawVolumeDescriptor({
        scalarData: indexVolume(),
        header: header(RAS, { qform_code: 0, sform_code: 0 }),
        source: SOURCE,
    });
    assert.equal(descriptor.orientationInferred, true);

    const declared = nativeRawVolumeDescriptor({
        scalarData: indexVolume(),
        header: header(RAS),
        source: SOURCE,
    });
    assert.equal(declared.orientationInferred, false);
});

// ---------------------------------------------------------------------------
// The segmentation fetch
// ---------------------------------------------------------------------------

function fakeFetch(calls, ok = true) {
    return async (url) => {
        calls.push(url);
        return { ok, status: ok ? 200 : 404, statusText: ok ? 'OK' : 'Not Found', arrayBuffer: async () => new ArrayBuffer(8) };
    };
}

test('the segmentation is fetched through the query-free bundle route', () => {
    const calls = [];
    return panorexSegmentationSource({
        panorexSource: { segmentationFileId: 7, segmentationFileKey: 'segmentation_nifti', revision: 2 },
        segmentationFile: null,
        cache: {},
        namespace: 'maxillo',
        origin: ORIGIN,
        fetchImpl: fakeFetch(calls),
    }).then((result) => {
        assert.equal(calls.length, 1);
        assert.match(calls[0], /\/maxillo\/api\/processing\/files\/serve\/7\/key\/segmentation_nifti\//);
        assert.ok(!calls[0].includes('?'), 'F14: no query string');
        assert.equal(result.descriptor.fileId, '7');
        assert.equal(result.descriptor.revision, 2);
    });
});

test('the segmentation is fetched once and memoised', async () => {
    const calls = [];
    const cache = {};
    const options = {
        panorexSource: { segmentationFileId: 7 },
        segmentationFile: null,
        cache,
        origin: ORIGIN,
        fetchImpl: fakeFetch(calls),
    };
    await panorexSegmentationSource(options);
    await panorexSegmentationSource(options);
    assert.equal(calls.length, 1, 'a segmentation volume must not be refetched per call');
});

test('the fallback segmentation file is used when the panorex source names none', async () => {
    const calls = [];
    const result = await panorexSegmentationSource({
        panorexSource: null,
        segmentationFile: { id: 11, file_key: 'segmentation_nifti' },
        cache: {},
        origin: ORIGIN,
        fetchImpl: fakeFetch(calls),
    });
    assert.match(calls[0], /serve\/11\/key\/segmentation_nifti\//);
    assert.equal(result.descriptor.fileId, '11');
});

test('no segmentation at all is an error, not an empty result', async () => {
    await assert.rejects(
        panorexSegmentationSource({ panorexSource: null, segmentationFile: null, cache: {} }),
        /No paired panoramic segmentation source/
    );
});

test('a failed fetch surfaces the status', async () => {
    await assert.rejects(
        panorexSegmentationSource({
            panorexSource: { segmentationFileId: 7 },
            segmentationFile: null,
            cache: {},
            origin: ORIGIN,
            fetchImpl: fakeFetch([], false),
        }),
        /HTTP 404/
    );
});

// ---------------------------------------------------------------------------
// The global shim
// ---------------------------------------------------------------------------

test('the bridge installs the three methods the panorex editor calls', () => {
    // cbct_panorex_editor.js:91, 149, 869 -- by name, on window.ViewerGrid.
    const target = {};
    installPanoramicBridge({
        getDescriptor: () => ({ marker: true }),
        data: { panorexSource: { revision: 4 }, segmentationFile: null },
        target,
    });

    for (const method of [
        'getNativeRawVolumeDescriptor',
        'getPanorexSegmentationSource',
        'getPanorexSourceDescriptor',
    ]) {
        assert.equal(typeof target.ViewerGrid[method], 'function', method);
    }
    assert.deepEqual(target.ViewerGrid.getNativeRawVolumeDescriptor(), { marker: true });
    assert.deepEqual(target.ViewerGrid.getPanorexSourceDescriptor(), { revision: 4 });
});

test('the bridge merges into an existing ViewerGrid rather than clobbering it', () => {
    // patient_detail.js and the panorex editor both reach for the global; replacing it
    // outright would be a load-order dependency nobody could see.
    const target = { ViewerGrid: { somethingElse: () => 'kept' } };
    installPanoramicBridge({ getDescriptor: () => null, data: {}, target });

    assert.equal(target.ViewerGrid.somethingElse(), 'kept');
    assert.equal(typeof target.ViewerGrid.getNativeRawVolumeDescriptor, 'function');
});

test('the ready event keeps its name and payload shape', () => {
    // viewer_grid.js:1408-1410 verbatim. The editor waits on this before it looks.
    const seen = [];
    const target = {
        dispatchEvent: (event) => seen.push(event),
        CustomEvent: globalThis.CustomEvent,
    };
    announceVolumeReady({ windowIndex: 0, modality: 'cbct', fileId: 42 }, target);

    assert.equal(VOLUME_READY_EVENT, 'viewergridvolumeready');
    assert.equal(seen.length, 1);
    assert.equal(seen[0].type, VOLUME_READY_EVENT);
    assert.deepEqual(seen[0].detail, { windowIndex: 0, modality: 'cbct', fileId: 42 });
});
