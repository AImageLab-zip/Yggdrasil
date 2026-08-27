import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';

const REPO = '/srv/Yggdrasil';

import {
    MAX_ANNOTATIONS,
    VIEWER_COORDINATE_SYSTEM,
    buildSaveRequest,
    interpretSaveResponse,
    volumeDescriptor,
} from '../imaging/grid/measurements.js';

const DECLARED_HEADER = {
    qform_code: 0,
    sform_code: 1,
    dims: [3, 512, 512, 400],
    pixDims: [1, 0.3, 0.3, 0.4],
    scl_slope: 1,
    scl_inter: -1024,
    affine: [
        [0.3, 0, 0, -76.8],
        [0, 0.3, 0, -76.8],
        [0, 0, 0.4, -80],
        [0, 0, 0, 1],
    ],
};

const ANNOTATION = {
    metadata: { toolName: 'Length', FrameOfReferenceUID: '1.2.3' },
    data: { handles: { points: [[0, 0, 0], [3, 4, 0]] } },
};

// ---------------------------------------------------------------------------
// The descriptor
// ---------------------------------------------------------------------------

test('the descriptor uses the same keys the maintenance sweep writes', () => {
    // Both writers populate SourceResource.descriptor. A second spelling would make
    // annotations_crosscheck compare a field against nothing.
    const descriptor = volumeDescriptor(DECLARED_HEADER);
    // `dtype` is the one key the sweep writes and this does not: after
    // `modalityScaleNifti` the cached array's type is a function of the rescale shape,
    // not of the file, so a viewer-reported dtype would describe Cornerstone's
    // promotion rather than the stored data. Absent beats wrong.
    assert.deepEqual(Object.keys(descriptor).sort(), [
        'affine',
        'orientation',
        'qform_code',
        'recorded_by',
        'scl_inter',
        'scl_slope',
        'sform_code',
        'shape',
        'spacing_mm',
        'spatial_codes_absent',
    ]);
});

test('the descriptor records the grid facts a resample would change', () => {
    const descriptor = volumeDescriptor(DECLARED_HEADER);
    assert.deepEqual(descriptor.shape, [512, 512, 400]);
    assert.deepEqual(descriptor.spacing_mm, [0.3, 0.3, 0.4]);
    assert.equal(descriptor.orientation, 'RAS');
    assert.equal(descriptor.spatial_codes_absent, false);
    assert.deepEqual(descriptor.affine, DECLARED_HEADER.affine);
});

test('the descriptor carries the two fields F1 turns on', () => {
    // Recorded so the modality LUT can be derived from the header rather than from a
    // loader that gates the rescale on `slope !== 1 && inter !== 0`.
    const descriptor = volumeDescriptor(DECLARED_HEADER);
    assert.equal(descriptor.scl_slope, 1);
    assert.equal(descriptor.scl_inter, -1024);

    // And an unset pair normalises rather than being stored as 0 or NaN.
    const unset = volumeDescriptor({ ...DECLARED_HEADER, scl_slope: 0, scl_inter: NaN });
    assert.equal(unset.scl_slope, 1);
    assert.equal(unset.scl_inter, 0);
});

test('the descriptor flags the F2 population the way the sweep spells it', () => {
    const inferred = volumeDescriptor({
        ...DECLARED_HEADER,
        qform_code: 0,
        sform_code: 0,
    });
    assert.equal(inferred.spatial_codes_absent, true);
    assert.equal(inferred.qform_code, 0);
    assert.equal(inferred.sform_code, 0);
});

// ---------------------------------------------------------------------------
// The request
// ---------------------------------------------------------------------------

test('a save request carries geometry and no measurement values', () => {
    // The server recomputes every number from the handles. A value sent from here
    // would be ignored at best and believed at worst.
    const body = buildSaveRequest({
        fileId: 42,
        annotations: [ANNOTATION],
        header: DECLARED_HEADER,
        expectedRevision: 3,
    });

    assert.equal(body.fileId, 42);
    assert.equal(body.expectedRevision, 3);
    assert.equal(body.coordinateSystem, VIEWER_COORDINATE_SYSTEM);
    assert.equal(body.annotations.length, 1);
    assert.ok(body.volumeDescriptor.shape);
    assert.ok(!('measurements' in body), 'the client must not send values');
});

test('a bundle member is named, and the plain sentinel is omitted', () => {
    const bundled = buildSaveRequest({
        fileId: 1,
        bundleKey: 'volume_nifti',
        annotations: [],
        header: DECLARED_HEADER,
        expectedRevision: 0,
    });
    assert.equal(bundled.fileKey, 'volume_nifti');

    // 'primary' is the sentinel for an ordinary row; the server reads an absent key
    // the same way, so it is not sent.
    for (const key of ['primary', undefined, null, '']) {
        const plain = buildSaveRequest({
            fileId: 1,
            bundleKey: key,
            annotations: [],
            header: DECLARED_HEADER,
            expectedRevision: 0,
        });
        assert.ok(!('fileKey' in plain), `bundleKey ${JSON.stringify(key)} must not be sent`);
    }
});

test('expectedRevision is required rather than defaulted to zero', () => {
    // Guessing means the second editor on a study loses a 409 they could have avoided.
    for (const bad of [undefined, null, -1, 1.5, '3']) {
        assert.throws(
            () =>
                buildSaveRequest({
                    fileId: 1,
                    annotations: [],
                    header: DECLARED_HEADER,
                    expectedRevision: bad,
                }),
            /expectedRevision is required/,
            `expectedRevision ${JSON.stringify(bad)} should be refused`
        );
    }
    assert.doesNotThrow(() =>
        buildSaveRequest({ fileId: 1, annotations: [], header: DECLARED_HEADER, expectedRevision: 0 })
    );
});

test('the client refuses an oversized save instead of sending it', () => {
    // Mirrors MAX_ANNOTATIONS_PER_REVISION in annotations/services/viewer.py. The
    // server would reject it whole; refusing here says so without the round trip.
    assert.equal(MAX_ANNOTATIONS, 500);
    assert.throws(
        () =>
            buildSaveRequest({
                fileId: 1,
                annotations: new Array(MAX_ANNOTATIONS + 1).fill(ANNOTATION),
                header: DECLARED_HEADER,
                expectedRevision: 0,
            }),
        /exceeds the 500/
    );
});

test('a bad fileId or annotation list is refused', () => {
    const base = { annotations: [], header: DECLARED_HEADER, expectedRevision: 0 };
    for (const fileId of [0, -1, 1.5, '1', undefined]) {
        assert.throws(() => buildSaveRequest({ ...base, fileId }), /positive integer/);
    }
    assert.throws(
        () => buildSaveRequest({ fileId: 1, annotations: {}, header: DECLARED_HEADER, expectedRevision: 0 }),
        /must be an array/
    );
});

// ---------------------------------------------------------------------------
// The response
// ---------------------------------------------------------------------------

test('a successful save reports the new revision', () => {
    const result = interpretSaveResponse({ ok: true, status: 200 }, { revision: 4 });
    assert.deepEqual(result, { saved: true, reload: false, revision: 4, message: null });
});

test('a 409 asks for a reload and never for a retry', () => {
    // Retrying with a bumped revision would overwrite the other editor's work, which
    // is exactly what the unique constraint exists to prevent.
    const result = interpretSaveResponse({ ok: false, status: 409 }, { conflict: true });
    assert.equal(result.saved, false);
    assert.equal(result.reload, true);
    assert.match(result.message, /has not been overwritten/);
    assert.match(result.message, /reload/);
});

test('any other failure surfaces the server message', () => {
    const result = interpretSaveResponse(
        { ok: false, status: 400 },
        { error: "no descriptor mapping for Cornerstone tool 'ArrowAnnotate'" }
    );
    assert.equal(result.saved, false);
    assert.equal(result.reload, false, 'a 400 is not a concurrency problem');
    assert.match(result.message, /ArrowAnnotate/);
});

test('a failure with no body still produces a message', () => {
    const result = interpretSaveResponse({ ok: false, status: 500 }, null);
    assert.match(result.message, /HTTP 500/);
});

// ---------------------------------------------------------------------------
// Which annotations are measurements
// ---------------------------------------------------------------------------

test('tool state that is not a measurement is not sent', async () => {
    // `getAllAnnotations()` returns everything Cornerstone holds, including the state
    // tools keep for themselves. CrosshairsTool is the one that bit: its annotation's
    // `data.handles` has `toolCenter` and `rotationPoints` and no `points` array, so
    // sending it made the server refuse the whole save with "a Cornerstone annotation
    // must carry at least one handle" -- about an annotation the user never drew.
    const { measurementAnnotations, MEASUREMENT_TOOLS } = await import(
        '../imaging/grid/measurements.js'
    );

    const crosshair = {
        metadata: { toolName: 'Crosshairs' },
        data: { handles: { toolCenter: [0, 0, 0], rotationPoints: [] } },
    };
    const length = {
        metadata: { toolName: 'Length' },
        data: { handles: { points: [[0, 0, 0], [1, 0, 0]] } },
    };

    assert.deepEqual(measurementAnnotations([crosshair, length]), [length]);
    assert.ok(!MEASUREMENT_TOOLS.includes('Crosshairs'));
    assert.ok(!MEASUREMENT_TOOLS.includes('WindowLevel'));
});

test('a malformed or empty annotation list yields nothing rather than throwing', async () => {
    const { measurementAnnotations } = await import('../imaging/grid/measurements.js');
    assert.deepEqual(measurementAnnotations([]), []);
    assert.deepEqual(measurementAnnotations(undefined), []);
    assert.deepEqual(measurementAnnotations([{}, { metadata: {} }, null]), []);
});

test('the client and the server agree on which tools are measurements', async () => {
    // Two lists of tool names in two languages. The server refuses anything it cannot
    // map, so a name here that is missing there fails the whole save -- and a name
    // there that is missing here is a measurement the user draws and never saves.
    const { MEASUREMENT_TOOLS } = await import('../imaging/grid/measurements.js');
    const adapter = await readFile(
        join(REPO, 'annotations', 'adapters', 'cornerstone.py'),
        'utf8'
    );

    const namesIn = (constant) => {
        const block = adapter.slice(adapter.indexOf(`${constant} = frozenset(`));
        return (block.slice(0, block.indexOf(')')).match(/"(\w+)"/g) ?? []).map((q) =>
            q.replaceAll('"', '')
        );
    };
    const server = [...namesIn('GEOMETRIC_TOOLS'), ...namesIn('INTENSITY_TOOLS')];

    assert.ok(server.length > 0, 'the adapter tool sets could not be parsed');
    assert.deepEqual([...MEASUREMENT_TOOLS].sort(), server.sort());
});
