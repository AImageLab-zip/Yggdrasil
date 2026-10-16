import test from 'node:test';
import assert from 'node:assert/strict';

import {
    assistSocketUrl,
    createAssistChannel,
    createPromptPoints,
    frameTimeMs,
    hasPixels,
    labelIsEmptyOn,
    maskTarget,
    mergeTrackedPlane,
    packMask,
    unpackMask,
} from '../imaging/segment/assist.js';

test('packMask is the worker bitpack_u1_v1: row-major, LSB-first, base64', () => {
    // numpy: np.packbits([1,0,0,0,0,0,0,0, 0,1], bitorder="little") == [1, 2] -> "AQI="
    assert.equal(packMask(Uint8Array.from([1, 0, 0, 0, 0, 0, 0, 0, 0, 1])), 'AQI=');
});

test('unpackMask inverts packMask on a frame that is not a multiple of 8', () => {
    const plane = Uint8Array.from({ length: 37 * 5 }, (_, index) => ((index * 7) % 3 === 0 ? 1 : 0));
    assert.deepEqual(unpackMask(packMask(plane), [5, 37], 37, 5), plane);
});

test('unpackMask refuses a mask for another frame size or orientation', () => {
    const mask = packMask(new Uint8Array(8 * 6));
    assert.throws(() => unpackMask(mask, [8, 6], 8, 6), /frame/, 'transposed: same byte count');
    assert.throws(() => unpackMask(packMask(new Uint8Array(64)), [8, 9], 9, 8), /frame/);
});

test('hasPixels', () => {
    assert.equal(hasPixels(new Uint8Array(4)), false);
    assert.equal(hasPixels(Uint8Array.from([0, 0, 1, 0])), true);
});

test('prompt points start over when the frame or the label changes', () => {
    const points = createPromptPoints();
    points.add(4, 'l1', 10, 20, 1);
    points.add(4, 'l1', 30, 40, 0);
    assert.deepEqual(points.for(4, 'l1'), [{ x: 10, y: 20, label: 1 }, { x: 30, y: 40, label: 0 }]);
    assert.deepEqual(points.for(5, 'l1'), [], 'another frame');
    assert.deepEqual(points.for(4, 'l1'), [], 'going back does not bring them back');
    points.add(4, 'l1', 1, 1, 1);
    assert.deepEqual(points.for(4, 'l2'), [], 'another label');
});

test('answered points are forgotten; points clicked meanwhile stay for the next prompt', () => {
    const points = createPromptPoints();
    points.add(4, 'l1', 10, 20, 1);
    const sent = [...points.for(4, 'l1')];
    points.add(4, 'l1', 30, 40, 0);
    points.forget(4, 'l1', sent);
    assert.deepEqual(points.for(4, 'l1'), [{ x: 30, y: 40, label: 0 }]);
});

test('a mask is written only where its prompt was sent from', () => {
    const sent = { frameIndex: 4, code: 'l1' };
    const here = { frameIndex: 4, code: 'l1' };
    assert.equal(maskTarget(sent, { frameIndex: 4 }, here), 'l1');
    assert.equal(maskTarget(sent, { frameIndex: 5 }, here), null, 'a frame the prompt was not for');
    assert.equal(maskTarget(sent, { frameIndex: 4 }, { frameIndex: 5, code: 'l1' }), null, 'moved to another frame');
    assert.equal(maskTarget(sent, { frameIndex: 4 }, { frameIndex: 4, code: 'l2' }), null, 'selected another label');
    assert.equal(maskTarget(null, { frameIndex: 4 }, here), null, 'nothing in flight');
});

test('assistSocketUrl follows the page scheme', () => {
    const data = { projectNamespace: 'laparoscopy', patientId: 3, fileId: 9 };
    assert.equal(assistSocketUrl({ protocol: 'https:', host: 'h' }, data), 'wss://h/ws/assist/laparoscopy/3/?fileId=9');
    assert.equal(assistSocketUrl({ protocol: 'http:', host: 'localhost:8000' }, data), 'ws://localhost:8000/ws/assist/laparoscopy/3/?fileId=9');
});

class FakeSocket {
    constructor(url) {
        this.url = url;
        this.sent = [];
        this.listeners = {};
        FakeSocket.last = this;
    }
    addEventListener(type, listener) {
        (this.listeners[type] ??= []).push(listener);
    }
    send(data) {
        this.sent.push(JSON.parse(data));
    }
    close() {
        this.emit('close', {});
    }
    emit(type, event) {
        for (const listener of this.listeners[type] ?? []) listener(event);
    }
    receive(message) {
        this.emit('message', { data: JSON.stringify(message) });
    }
}

function channel(overrides = {}) {
    const seen = { statuses: [], masks: [], errors: [] };
    const assist = createAssistChannel({
        url: 'ws://x',
        WebSocketImpl: FakeSocket,
        onStatus: (status) => seen.statuses.push(status),
        onMask: (message, context) => seen.masks.push([message.frameIndex, context]),
        onError: (text) => seen.errors.push(text),
        ...overrides,
    });
    return { assist, socket: FakeSocket.last, seen };
}

test('a prompt is built when the channel is free, from what is still unanswered then', () => {
    const { assist, socket, seen } = channel();
    let pending = [1];
    const build = () => (pending.length ? { payload: { points: [...pending] }, context: [...pending] } : null);
    assist.request(build);
    assert.equal(socket.sent.length, 0, 'nothing before the worker is ready');
    socket.receive({ type: 'preparing' });
    socket.receive({ type: 'ready' });
    assert.deepEqual(socket.sent, [{ type: 'prompt', points: [1] }]);
    pending.push(2);
    assist.request(build);
    pending.push(3);
    assist.request(build);
    assert.equal(socket.sent.length, 1, 'one prompt in flight');
    socket.receive({ type: 'mask', frameIndex: 0 });
    pending = pending.filter((point) => point !== 1); // the answered point is forgotten
    socket.receive({ type: 'done' });
    assert.deepEqual(socket.sent, [{ type: 'prompt', points: [1] }, { type: 'prompt', points: [2, 3] }]);
    assert.deepEqual(seen.masks, [[0, [1]]]);
    assert.deepEqual(seen.statuses.slice(0, 3), ['preparing', 'ready', 'busy']);
});

test('nothing is sent when there is nothing left to ask', () => {
    const { assist, socket } = channel();
    socket.receive({ type: 'ready' });
    assist.request(() => null);
    assert.equal(socket.sent.length, 0);
});

test('an error ends the prompt in flight and is reported', () => {
    const { assist, socket, seen } = channel();
    socket.receive({ type: 'ready' });
    assist.request(() => ({ payload: { frameIndex: 1 }, context: 'a' }));
    assist.request(() => ({ payload: { frameIndex: 2 }, context: 'b' }));
    socket.receive({ type: 'error', message: 'The assistant could not segment this frame.' });
    assert.deepEqual(seen.errors, ['The assistant could not segment this frame.']);
    assert.deepEqual(socket.sent.at(-1), { type: 'prompt', frameIndex: 2 });
});

test('a closed socket says so and sends nothing more', () => {
    const { assist, socket, seen } = channel();
    socket.receive({ type: 'ready' });
    socket.close();
    assert.equal(assist.status, 'closed');
    assert.equal(seen.statuses.at(-1), 'closed');
    assist.request(() => ({ payload: { frameIndex: 1 }, context: 'a' }));
    assert.equal(socket.sent.length, 0);
});

test('a tracked frame is stored under the same time key as frameSource uses', () => {
    assert.equal(frameTimeMs(7, 1), 7000);
    assert.equal(frameTimeMs(1, 3), 333, 'round(n * 1000 / fps)');
});

test('tracking fills only frames where the label is still empty', () => {
    const frames = { 3000: { labels: { l1: { bbox: [0, 0, 1, 1] } } }, 4000: { labels: { l2: {} } } };
    assert.equal(labelIsEmptyOn(frames, 3000, 'l1'), false, 'already annotated: kept');
    assert.equal(labelIsEmptyOn(frames, 4000, 'l1'), true, 'another label only');
    assert.equal(labelIsEmptyOn(frames, 5000, 'l1'), true, 'nothing stored');
    assert.equal(labelIsEmptyOn(undefined, 5000, 'l1'), true);
});

test('a tracked plane joins the stored planes and never replaces a painted one', () => {
    const other = Uint8Array.from([1, 0, 0, 0]);
    const tracked = Uint8Array.from([0, 1, 1, 0]);
    assert.deepEqual(mergeTrackedPlane({ l2: other }, 'l1', tracked), { l2: other, l1: tracked });
    const painted = Uint8Array.from([0, 0, 0, 1]);
    assert.deepEqual(mergeTrackedPlane({ l1: painted }, 'l1', tracked), { l1: painted });
    assert.deepEqual(mergeTrackedPlane({ l1: new Uint8Array(4) }, 'l1', tracked), { l1: tracked }, 'an empty stored plane is replaced');
});
