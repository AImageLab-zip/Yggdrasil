import test from 'node:test';
import assert from 'node:assert/strict';

import { dicomSeriesUrl, prepareDicomSeries } from '../imaging/grid/dicomVolume.js';

const ORIGIN = 'https://ygg.example';
const STUDY = '2.25.111';
const SERIES = '2.25.222';

function instance({ sop, number, z, frames = 1 }) {
    const document = {
        '00080018': { vr: 'UI', Value: [sop] },
        '00080060': { vr: 'CS', Value: ['CT'] },
        '00200013': { vr: 'IS', Value: [number] },
        '00200032': { vr: 'DS', Value: [0, 0, z] },
        '00200037': { vr: 'DS', Value: [1, 0, 0, 0, 1, 0] },
        '00280010': { vr: 'US', Value: [4] },
        '00280011': { vr: 'US', Value: [4] },
        '00280030': { vr: 'DS', Value: [0.25, 0.25] },
    };
    if (frames > 1) {
        document['00280008'] = { vr: 'IS', Value: [frames] };
    }
    return document;
}

function fakeFetch(instances, { ok = true, status = 200 } = {}) {
    const calls = [];
    const impl = async (url) => {
        calls.push(url);
        return { ok, status, json: async () => instances };
    };
    impl.calls = calls;
    return impl;
}

function recorder() {
    const added = [];
    return { added, add: (imageId, document) => added.push({ imageId, document }) };
}

const SERIES_OF_THREE = [
    instance({ sop: '2.25.30', number: 3, z: 0.8 }),
    instance({ sop: '2.25.10', number: 1, z: 0 }),
    instance({ sop: '2.25.20', number: 2, z: 0.4 }),
];

test('the series URL is the metadata endpoint, and doubles as the cache key', () => {
    assert.equal(
        dicomSeriesUrl({ studyUid: STUDY, seriesUid: SERIES, origin: ORIGIN }),
        `${ORIGIN}/api/dicomweb/studies/${STUDY}/series/${SERIES}/metadata`
    );
});

test('metadata is fetched once for the whole series, not once per instance', async () => {
    // On a 400-slice CBCT this is the difference between one round trip and 400.
    const fetchImpl = fakeFetch(SERIES_OF_THREE);
    await prepareDicomSeries({
        studyUid: STUDY, seriesUid: SERIES, metaDataManager: recorder(), fetchImpl, origin: ORIGIN,
    });
    assert.equal(fetchImpl.calls.length, 1);
});

test('every imageId gets its own instance document registered', async () => {
    // The wadors loader never fetches metadata; an unregistered id fails inside the
    // decoder with no mention of what is missing.
    const manager = recorder();
    const { imageIds } = await prepareDicomSeries({
        studyUid: STUDY, seriesUid: SERIES, metaDataManager: manager,
        fetchImpl: fakeFetch(SERIES_OF_THREE), origin: ORIGIN,
    });
    assert.equal(manager.added.length, imageIds.length);
    assert.deepEqual(manager.added.map((e) => e.imageId), imageIds);
});

test('an id is registered with the document it was built from', async () => {
    const manager = recorder();
    await prepareDicomSeries({
        studyUid: STUDY, seriesUid: SERIES, metaDataManager: manager,
        fetchImpl: fakeFetch(SERIES_OF_THREE), origin: ORIGIN,
    });
    for (const { imageId, document } of manager.added) {
        assert.ok(imageId.includes(`/instances/${document['00080018'].Value[0]}/`));
    }
});

test('imageIds come back in slice order', async () => {
    const { imageIds } = await prepareDicomSeries({
        studyUid: STUDY, seriesUid: SERIES, metaDataManager: recorder(),
        fetchImpl: fakeFetch(SERIES_OF_THREE), origin: ORIGIN,
    });
    assert.deepEqual(
        imageIds.map((id) => id.split('/instances/')[1].split('/')[0]),
        ['2.25.10', '2.25.20', '2.25.30']
    );
});

test('the header is built from unique documents, not one per frame', async () => {
    // dicomSeriesHeader counts frames from NumberOfFrames. Feeding it one entry per
    // frame would double-count a multi-frame series and halve its slice spacing.
    const { header, imageIds } = await prepareDicomSeries({
        studyUid: STUDY, seriesUid: SERIES, metaDataManager: recorder(),
        fetchImpl: fakeFetch([
            instance({ sop: '2.25.10', number: 1, z: 0, frames: 2 }),
            instance({ sop: '2.25.20', number: 2, z: 1, frames: 2 }),
        ]),
        origin: ORIGIN,
    });
    assert.equal(imageIds.length, 4);
    assert.equal(header.dims[3], 4);
    assert.equal(header.pixDims[3], 1);
});

test('a refused fetch says something a reader can act on', async () => {
    await assert.rejects(
        prepareDicomSeries({
            studyUid: STUDY, seriesUid: SERIES, metaDataManager: recorder(),
            fetchImpl: fakeFetch([], { ok: false, status: 404 }), origin: ORIGIN,
        }),
        /HTTP 404/
    );
});

test('an empty series is an error rather than a blank viewport', async () => {
    await assert.rejects(
        prepareDicomSeries({
            studyUid: STUDY, seriesUid: SERIES, metaDataManager: recorder(),
            fetchImpl: fakeFetch([]), origin: ORIGIN,
        }),
        /no instances stored/
    );
});
