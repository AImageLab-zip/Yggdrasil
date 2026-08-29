import test from 'node:test';
import assert from 'node:assert/strict';

import {
    ALGORITHM_VERSION,
    ANNOUNCE_TYPE,
    OUTCOMES,
    announcement,
    canRestore,
    generationUuid,
    hasSavedPanoramic,
    interpretSave,
    saveBody,
    saveState,
} from '../imaging/panoramic/savePayload.js';
import { COLUMNS_PER_CHUNK, projectStrips } from '../imaging/panoramic/bake.js';
import { openArchWorker } from '../imaging/panoramic/archWorker.js';

const SOURCE = {
    jobId: 7,
    volumeFileId: 12,
    volumeFileKey: 'volume_nifti',
    volumeFileHash: 'a'.repeat(64),
    segmentationFileId: 13,
    segmentationFileKey: 'segmentation_nifti',
    segmentationFileHash: 'b'.repeat(64),
    revision: 3,
};
const DIMENSIONS = { width: 100, height: 80, depth: 40 };

test('the save body is the shape the endpoint validates', () => {
    const state = saveState({
        source: SOURCE,
        dimensions: DIMENSIONS,
        axialSlice: 20,
        controlPoints: [[1, 2], [3, 4], [5, 6], [7, 8]],
        geometrySource: 'custom_cp',
        mode: 'raysum',
        generationUuid: 'uuid-1',
    });

    assert.deepEqual(state.source, {
        job_id: 7,
        file_id: 12,
        file_key: 'volume_nifti',
        file_hash: 'a'.repeat(64),
        segmentation_file_id: 13,
        segmentation_file_key: 'segmentation_nifti',
        segmentation_file_hash: 'b'.repeat(64),
    });
    assert.deepEqual(state.volume_shape, [100, 80, 40]);
    assert.equal(state.axial_slice, 20);
    assert.equal(state.geometry_source, 'custom_cp');
    assert.equal(state.default_mode, 'raysum');
    assert.equal(state.algorithm_version, ALGORITHM_VERSION);
    assert.equal(state.base_revision, 3);
});

test('the arch is copied into the body, not handed over by reference', () => {
    const controlPoints = [[1, 2], [3, 4], [5, 6], [7, 8]];

    const state = saveState({
        source: SOURCE, dimensions: DIMENSIONS, axialSlice: 1, controlPoints,
        geometrySource: 'auto', mode: 'mip', generationUuid: 'uuid-1',
    });
    controlPoints[0][0] = 999;

    // The editor keeps dragging its own array; a request holding a reference to it would
    // post whatever the arch looked like when the upload happened to serialize.
    assert.deepEqual(state.spline[0], [1, 2]);
});

test('a patient with no arch quotes revision 0', () => {
    const state = saveState({
        source: { ...SOURCE, revision: 0 }, dimensions: DIMENSIONS, axialSlice: 1,
        controlPoints: [[1, 2], [3, 4], [5, 6], [7, 8]], geometrySource: 'auto',
        mode: 'mip', generationUuid: 'uuid-1',
    });

    assert.equal(state.base_revision, 0);
});

test('the multipart body names both strips the way the endpoint reads them', () => {
    const appended = [];
    class FakeFormData {
        append(...args) { appended.push(args); }
    }

    saveBody({ ok: true }, { mip: 'MIP', raysum: 'RAY' }, FakeFormData);

    assert.deepEqual(appended.map((entry) => entry[0]), ['state', 'mip_png', 'raysum_png']);
    assert.equal(appended[0][1], '{"ok":true}');
    assert.deepEqual(appended[1].slice(1), ['MIP', 'panoramic-mip.png']);
    assert.deepEqual(appended[2].slice(1), ['RAY', 'panoramic-raysum.png']);
});

test('a silent 409 means somebody already did it, not that it failed', () => {
    // A warm-up run over an already-warmed folder would otherwise come back a wall of red.
    const result = interpretSave({ status: 409, ok: false }, { error: 'Stale' }, true);

    assert.equal(result.saved, true);
    assert.equal(result.conflicted, true);
    assert.equal(result.outcome, OUTCOMES.EXISTING);
});

test('the same 409 in front of a reader is a failure with its message', () => {
    const result = interpretSave({ status: 409, ok: false }, { error: 'Stale panoramic revision' }, false);

    assert.equal(result.saved, false);
    assert.equal(result.outcome, OUTCOMES.FAILED);
    assert.equal(result.message, 'Stale panoramic revision');
});

test('a body-less failure still says something a person can act on', () => {
    const result = interpretSave({ status: 500, ok: false }, null, false);

    assert.match(result.message, /HTTP 500/);
});

test('a success reports the revision the client must quote next', () => {
    const result = interpretSave({ status: 200, ok: true }, { revision: 4 }, false);

    assert.equal(result.saved, true);
    assert.equal(result.outcome, OUTCOMES.CREATED);
    assert.equal(result.revision, 4);
});

test('the announcement carries what the warm-up switches on', () => {
    // `static/js/panoramic_warmup.js:158-163` reads exactly these three fields, and a page
    // that never sends one costs the run a five-minute timeout for that patient.
    assert.deepEqual(announcement(OUTCOMES.SKIPPED, 42, 'no viewer grid'), {
        type: ANNOUNCE_TYPE,
        outcome: 'skipped',
        patientId: 42,
        detail: 'no viewer grid',
    });
    assert.deepEqual(announcement(OUTCOMES.CREATED, null), {
        type: ANNOUNCE_TYPE, outcome: 'created', patientId: null, detail: null,
    });
});

test('a stored arch is restorable only when it still describes this volume', () => {
    const good = {
        algorithmVersion: ALGORITHM_VERSION,
        spline: [[1, 1], [2, 2], [3, 3], [4, 4]],
        volumeShape: [100, 80, 40],
    };

    assert.equal(canRestore(good, DIMENSIONS), true);
    assert.equal(canRestore(null, DIMENSIONS), false);
    // A superseded baker's arch is history, not something to reopen.
    assert.equal(canRestore({ ...good, algorithmVersion: 'panorex-js-v1' }, DIMENSIONS), false);
    // The endpoint refuses fewer than four on the way in, and the chain needs four to draw.
    assert.equal(canRestore({ ...good, spline: [[1, 1], [2, 2], [3, 3]] }, DIMENSIONS), false);
    // Drawn on a different volume, whatever the revision says.
    assert.equal(canRestore({ ...good, volumeShape: [100, 80, 41] }, DIMENSIONS), false);
});

test('a current panoramic is one with a revision and this baker', () => {
    const state = { algorithmVersion: ALGORITHM_VERSION };

    assert.equal(hasSavedPanoramic({ revision: 2, state }), true);
    assert.equal(hasSavedPanoramic({ revision: 0, state }), false);
    assert.equal(hasSavedPanoramic({ revision: 2, state: null }), false);
    assert.equal(hasSavedPanoramic({ revision: 2, state: { algorithmVersion: 'old' } }), false);
    assert.equal(hasSavedPanoramic(null), false);
});

test('a generation uuid is minted without the platform helper too', () => {
    const uuid = generationUuid({
        getRandomValues: (bytes) => { bytes.fill(0xab); return bytes; },
    });

    assert.match(uuid, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
});

// --------------------------------------------------------------------- baking

/** A core that records what it was asked to project, without doing any arithmetic. */
function fakeCore() {
    const columns = [];
    return {
        columns,
        projectColumnPair(data, dimensions, slab, column, mip, raysum, flipZ, slope, intercept) {
            columns.push({ column, flipZ, slope, intercept });
            mip[column] = column;
            raysum[column] = -column;
        },
        normalizeOpenCV: (values) => Uint8Array.from(values, (value) => Math.trunc(Math.abs(value))),
    };
}

const DESCRIPTOR = {
    data: new Float32Array(8),
    dimensions: { width: 2, height: 2, depth: 3 },
    flipZ: true,
    slope: 2,
    intercept: -1024,
};

test('every column is projected, with the rescale the descriptor carries', async () => {
    const core = fakeCore();
    const slab = Array.from({ length: 9 }, () => [[0, 0], [1, 1]]);

    const result = await projectStrips({
        descriptor: DESCRIPTOR, slab, core, schedule: (callback) => callback(),
    });

    assert.equal(result.width, 9);
    assert.equal(result.height, 3);
    assert.deepEqual(core.columns.map((entry) => entry.column), [0, 1, 2, 3, 4, 5, 6, 7, 8]);
    // Not defaults: an F1-affected volume is rescaled by the descriptor's own slope and
    // intercept, and dropping them would bake every voxel 1024 HU out.
    assert.deepEqual(core.columns[0], { column: 0, flipZ: true, slope: 2, intercept: -1024 });
});

test('the bake yields between chunks rather than freezing the tab', async () => {
    const core = fakeCore();
    const slab = Array.from({ length: 10 }, () => [[0, 0], [1, 1]]);
    let ticks = 0;

    await projectStrips({
        descriptor: DESCRIPTOR, slab, core,
        schedule: (callback) => { ticks += 1; callback(); },
    });

    // One tick per chunk, the last of which resolves rather than rescheduling. A single
    // pass would be one tick, and a CBCT's worth of columns in one tick is seconds of
    // unresponsive page with no way to cancel it.
    assert.equal(ticks, Math.ceil(10 / COLUMNS_PER_CHUNK));
});

test('a superseded bake stops instead of finishing and winning', async () => {
    const core = fakeCore();
    const slab = Array.from({ length: 20 }, () => [[0, 0], [1, 1]]);
    let started = false;

    const result = await projectStrips({
        descriptor: DESCRIPTOR, slab, core,
        // Cancelled after the first chunk, as a second drag would.
        cancelled: () => { const was = started; started = true; return was; },
        schedule: (callback) => callback(),
    });

    assert.equal(result, null);
    assert.ok(core.columns.length < 20, 'it stopped early');
});

test('an arch with no columns is refused rather than baked empty', async () => {
    await assert.rejects(
        projectStrips({ descriptor: DESCRIPTOR, slab: [], core: fakeCore() }),
        /no columns/
    );
});

// -------------------------------------------------------------- worker client

/** A worker that records what it was posted and lets a test reply on its behalf. */
function fakeWorker() {
    const posted = [];
    let instance = null;
    class FakeWorker {
        constructor(url) {
            this.url = url;
            this.terminated = false;
            instance = this;
        }
        postMessage(message) { posted.push(message); }
        terminate() { this.terminated = true; }
    }
    return { posted, FakeWorker, reply: (data) => instance.onmessage({ data }), get instance() { return instance; } };
}

const RAW = { dimensions: { width: 2, height: 2, depth: 3 }, affine: [[1, 0, 0, 0]], flipZ: false };

test('the segmentation is transferred as a copy, so it survives a reopen', () => {
    const { posted, FakeWorker } = fakeWorker();
    const segmentation = new ArrayBuffer(64);

    openArchWorker({ segmentation, raw: RAW, WorkerImpl: FakeWorker });

    assert.equal(posted[0].type, 'init');
    assert.notEqual(posted[0].buffer, segmentation);
    assert.equal(posted[0].buffer.byteLength, 64);
    // The caller's copy is still readable: `pagehide` tears the worker down, and a
    // transferred original would come back detached and zero-length.
    assert.equal(segmentation.byteLength, 64);
});

test('a stale geometry reply is dropped', () => {
    const { FakeWorker, reply } = fakeWorker();
    const seen = [];
    const client = openArchWorker({
        segmentation: new ArrayBuffer(8), raw: RAW, WorkerImpl: FakeWorker,
        onGeometry: (geometry) => seen.push(geometry.z),
    });
    reply({ type: 'initialized', id: 1, dimensions: RAW.dimensions, autoZ: 5, flipZ: false });

    const first = client.request(5);
    client.request(9);
    reply({ type: 'geometry', id: first, z: 5, slab: [] });

    // Dragging the arch re-requests; a slow reply for the previous arch would otherwise
    // redraw the one the reader has already moved on from.
    assert.deepEqual(seen, []);
});

test('an error before initialize is fatal, and one after is not', () => {
    const { FakeWorker, reply } = fakeWorker();
    const errors = [];
    const client = openArchWorker({
        segmentation: new ArrayBuffer(8), raw: RAW, WorkerImpl: FakeWorker,
        onError: (error, fatal) => errors.push(fatal),
    });

    reply({ type: 'error', id: 1, message: 'no segmentation' });
    reply({ type: 'initialized', id: 1, dimensions: RAW.dimensions, autoZ: 5, flipZ: false });
    const id = client.request(5);
    reply({ type: 'error', id, message: 'the arch could not be fitted' });

    // No segmentation means there is nothing to fit against at all; a failed fit on one
    // slice says nothing about the next one.
    assert.deepEqual(errors, [true, false]);
});

test('the ready promise reports the automatic slice the worker chose', async () => {
    const { FakeWorker, reply } = fakeWorker();
    const client = openArchWorker({
        segmentation: new ArrayBuffer(8), raw: RAW, WorkerImpl: FakeWorker,
    });

    reply({ type: 'initialized', id: 1, dimensions: RAW.dimensions, autoZ: 96, flipZ: true });

    assert.deepEqual(await client.ready, { dimensions: RAW.dimensions, autoZ: 96, flipZ: true });
});
