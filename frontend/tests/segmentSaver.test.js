import test from 'node:test';
import assert from 'node:assert/strict';

import { SaveStatus, createSaver } from '../imaging/segment/saver.js';
import { ApiError, createApi, endpointFor } from '../imaging/segment/api.js';
import { describeSave, labelAfterRemoval, opacityToPercent, percentToOpacity } from '../imaging/segment/labelPanel.js';
import { buildSaveBody, readSegmentData } from '../imaging/segment/bootstrap.js';
import { defaultBrushSize, maxBrushSize } from '../imaging/segment/session.js';
import { FIRST_LABEL_NAME } from '../imaging/segment/bootstrap.js';
import { ALL_TOOL_NAMES, SEGMENT_TOOLS, forEachInCapsule, pointInPolygon, polygonBounds, toolNameFor } from '../imaging/segment/tools.js';
import { buildColorLut, hexToRgb, segmentationIdFor } from '../imaging/segment/labelmapStack.js';

function timers() {
    const pending = [];
    return {
        setTimeoutImpl: (fn) => pending.push(fn) && pending.length,
        clearTimeoutImpl: (id) => {
            pending[id - 1] = null;
        },
        async fire() {
            const due = pending.splice(0).filter(Boolean);
            for (const fn of due) await fn();
            await new Promise((resolve) => setImmediate(resolve));
        },
        get armed() {
            return pending.filter(Boolean).length;
        },
    };
}

const gate = () => {
    let release;
    const promise = new Promise((resolve) => {
        release = resolve;
    });
    return { promise, release };
};

test('edits debounce into one save that quotes the loaded revision', async () => {
    const clock = timers();
    const calls = [];
    const saver = createSaver({
        revision: 4,
        send: async (keys, revision) => {
            calls.push({ keys, revision });
            return { revision: revision + 1 };
        },
        ...clock,
    });
    saver.markDirty(100);
    saver.markDirty(100);
    saver.markDirty(200);
    assert.equal(clock.armed, 1, 'each edit re-arms a single timer');
    await clock.fire();
    assert.deepEqual(calls, [{ keys: ['100', '200'], revision: 4 }]);
    assert.equal(saver.revision, 5);
    assert.equal(saver.status, SaveStatus.CLEAN);
});

test('an edit made while a save is in flight is saved afterwards, not lost', async () => {
    const clock = timers();
    const first = gate();
    const sent = [];
    const saver = createSaver({
        send: async (keys, revision) => {
            sent.push([...keys]);
            if (sent.length === 1) await first.promise;
            return { revision: revision + 1 };
        },
        ...clock,
    });
    saver.markDirty(1);
    const flight = clock.fire();
    await new Promise((resolve) => setImmediate(resolve));
    saver.markDirty(1); // same key, edited again mid-flight
    first.release();
    await flight;
    assert.equal(saver.status, SaveStatus.DIRTY, 'the mid-flight edit keeps it dirty');
    await clock.fire();
    assert.deepEqual(sent, [['1'], ['1']]);
    assert.equal(saver.status, SaveStatus.CLEAN);
});

test('a 409 stops autosave for good', async () => {
    const clock = timers();
    let calls = 0;
    const saver = createSaver({
        send: async () => {
            calls += 1;
            throw new ApiError('stale', { status: 409, conflict: true });
        },
        ...clock,
    });
    saver.markDirty(1);
    await clock.fire();
    assert.equal(saver.status, SaveStatus.CONFLICT);
    saver.markDirty(2);
    assert.equal(clock.armed, 0, 'no further autosave is armed');
    await saver.flush();
    assert.equal(calls, 1);
});

test('any other failure keeps the work dirty and does not retry by itself', async () => {
    const clock = timers();
    let calls = 0;
    const saver = createSaver({
        send: async () => {
            calls += 1;
            if (calls === 1) throw new Error('offline');
            return { revision: 1 };
        },
        ...clock,
    });
    saver.markDirty(1);
    await clock.fire();
    assert.equal(saver.status, SaveStatus.ERROR);
    assert.equal(saver.pending, 1);
    assert.equal(clock.armed, 0);
    await saver.flush();
    assert.equal(saver.status, SaveStatus.CLEAN);
    assert.equal(calls, 2);
});

test('flush saves immediately and cancels the debounce', async () => {
    const clock = timers();
    const saver = createSaver({ send: async () => ({ revision: 1 }), ...clock });
    saver.markDirty(1);
    await saver.flush();
    assert.equal(clock.armed, 0);
    assert.equal(saver.status, SaveStatus.CLEAN);
});

function fakeFetch(responses) {
    const calls = [];
    const impl = async (url, init) => {
        calls.push({ url, init });
        const next = responses.shift();
        return {
            ok: next.status < 400,
            status: next.status,
            json: async () => {
                if (next.body === undefined) throw new Error('not json');
                return next.body;
            },
        };
    };
    return { impl, calls };
}

test('writes carry the CSRF token and a JSON body; reads carry neither', async () => {
    const { impl, calls } = fakeFetch([
        { status: 200, body: { revision: 3 } },
        { status: 200, body: {} },
    ]);
    const api = createApi({ endpoint: '/laparoscopy/api/patients/2/image-segmentation/', fetchImpl: impl, csrf: 'tok' });
    await api.save({ a: 1 });
    await api.state();
    assert.equal(calls[0].init.method, 'POST');
    assert.equal(calls[0].init.headers['X-CSRFToken'], 'tok');
    assert.equal(calls[0].init.body, '{"a":1}');
    assert.equal(calls[1].init.headers['X-CSRFToken'], undefined);
    assert.equal(calls[0].init.credentials, 'same-origin');
});

test('a 409 is a conflict, a 404 frame is null, and messages come from the server', async () => {
    const { impl } = fakeFetch([
        { status: 409, body: { error: 'revision 5 exists', conflict: true } },
        { status: 404, body: { error: 'No mask' } },
        { status: 400, body: { error: 'unknown label' } },
        { status: 500 },
    ]);
    const api = createApi({ endpoint: '/x/', fetchImpl: impl, csrf: '' });
    await assert.rejects(api.save({}), (error) => error.conflict === true && /revision 5/.test(error.message));
    assert.equal(await api.frame(1, 2), null);
    await assert.rejects(api.save({}), /unknown label/);
    await assert.rejects(api.save({}), (error) => error.status === 500 && /500/.test(error.message));
});

test('a network failure is an ApiError, not a bare TypeError', async () => {
    const api = createApi({
        endpoint: '/x/',
        fetchImpl: async () => {
            throw new TypeError('Failed to fetch');
        },
    });
    await assert.rejects(api.state(), (error) => error instanceof ApiError && /Could not reach/.test(error.message));
});

test('endpoint and body builders', () => {
    assert.equal(endpointFor({ projectNamespace: 'laparoscopy', patientId: 7 }), '/laparoscopy/api/patients/7/image-segmentation/');
    assert.deepEqual(buildSaveBody({ fileId: 2, revision: 4, fps: 30, frames: [] }), {
        fileId: 2,
        expectedRevision: 4,
        coordinateSystem: 'video_pixel',
        fps: 30,
        frames: [],
    });
});

test('the page payload is refused without the probe', () => {
    const doc = (json) => ({ getElementById: () => ({ textContent: json }) });
    const good = { patientId: 1, projectNamespace: 'laparoscopy', fileId: 2, videoUrl: '/laparoscopy/api/processing/files/serve/2/', frameCount: 300, fps: 30, width: 1920, height: 1080 };
    assert.deepEqual(readSegmentData(doc(JSON.stringify(good))), good);
    for (const bad of [{ ...good, fps: 0 }, { ...good, width: undefined }, { ...good, fileId: '2' }, {}]) {
        assert.equal(readSegmentData(doc(JSON.stringify(bad))), null);
    }
    assert.equal(readSegmentData(doc('{')), null);
    assert.equal(readSegmentData({ getElementById: () => null }), null);
});

test('point-in-polygon: inside, outside, concave', () => {
    const square = [[0, 0], [10, 0], [10, 10], [0, 10]];
    assert.equal(pointInPolygon(5, 5, square), true);
    assert.equal(pointInPolygon(11, 5, square), false);
    const notch = [[0, 0], [10, 0], [10, 10], [5, 4], [0, 10]];
    assert.equal(pointInPolygon(5, 8, notch), false, 'inside the notch is outside the shape');
    assert.equal(pointInPolygon(5, 2, notch), true);
});

test('polygon bounds are clamped to the image, and empty off it', () => {
    assert.deepEqual(polygonBounds([[-5, -5], [20, 3]], [10, 10]), [[0, 9], [0, 3], [0, 0]]);
    assert.equal(polygonBounds([[50, 50], [60, 60]], [10, 10]), null);
    assert.equal(polygonBounds([], [10, 10]), null);
});

test('every toolbar entry maps to a registered tool name', () => {
    assert.equal(toolNameFor('brush'), 'Brush');
    assert.equal(toolNameFor('eraser'), 'BrushErase');
    assert.equal(toolNameFor('polygon'), 'PolygonFill');
    assert.throws(() => toolNameFor('lasso'), /Unknown/);
    assert.equal(toolNameFor('brush', { erase: true }), 'Brush', 'the brush has its own eraser button');
    assert.throws(() => toolNameFor('wand'), /Unknown/);
    for (const tool of SEGMENT_TOOLS) {
        assert.ok(ALL_TOOL_NAMES.includes(tool.fill));
    }
    assert.equal(new Set(ALL_TOOL_NAMES).size, ALL_TOOL_NAMES.length);
});

test('label colours become a LUT in which only segment 1 shows', () => {
    assert.deepEqual(hexToRgb('#3498db'), [52, 152, 219]);
    assert.throws(() => hexToRgb('blue'), /#rrggbb/);
    const lut = buildColorLut('#ff0000');
    assert.equal(lut.length, 256);
    assert.deepEqual(lut[0], [0, 0, 0, 0]);
    assert.deepEqual(lut[1], [255, 0, 0, 255]);
    assert.deepEqual(lut[2], [0, 0, 0, 0]);
    assert.equal(segmentationIdFor('vp', 'l3'), 'vp:l3');
});

test('label panel helpers', () => {
    assert.equal(opacityToPercent(0.5), 50);
    assert.equal(opacityToPercent(7), 100);
    assert.equal(percentToOpacity('25'), 0.25);
    const labels = [
        { code: 'l1', active: true },
        { code: 'l2', active: false },
        { code: 'l3', active: true },
    ];
    assert.equal(labelAfterRemoval(labels, 'l1'), 'l3');
    assert.equal(labelAfterRemoval([{ code: 'l1', active: true }], 'l1'), null);
    assert.match(describeSave('conflict'), /Reload/);
    assert.equal(describeSave('clean', { revision: 3 }), 'Saved (revision 3)');
    assert.equal(describeSave('dirty', { pending: 1 }), 'Unsaved changes');
});

test('the default brush scales with the frame, so a stroke can be seen', () => {
    assert.equal(defaultBrushSize(1920), 16);
    assert.ok(defaultBrushSize(320) >= 4, 'never smaller than a visible dab');
    assert.ok(maxBrushSize(1920) > defaultBrushSize(1920));
    assert.equal(FIRST_LABEL_NAME, 'Label 1');
});

test('a capsule covers the disc at each end and the band between', () => {
    const hit = new Set();
    forEachInCapsule([100, 50], [10, 25], [60, 25], 5, (i) => hit.add(i));
    assert.ok(hit.has(25 * 100 + 10) && hit.has(25 * 100 + 35) && hit.has(25 * 100 + 60));
    assert.ok(hit.has(30 * 100 + 35) && !hit.has(31 * 100 + 35), 'radius 5 either side of the line');
    assert.ok(hit.has(25 * 100 + 5) && !hit.has(25 * 100 + 4));
    const clipped = [];
    forEachInCapsule([20, 20], [-50, -50], [5, 5], 8, (i) => clipped.push(i));
    assert.ok(clipped.every((i) => i >= 0 && i < 400), 'stays inside the image');
    const dab = [];
    forEachInCapsule([20, 20], [10, 10], [10, 10], 2, (i) => dab.push(i));
    assert.equal(dab.length, 13, 'a zero-length stroke is a disc');
});
