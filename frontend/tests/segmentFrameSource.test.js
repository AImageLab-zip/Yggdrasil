import test from 'node:test';
import assert from 'node:assert/strict';

import { createFrameSource, snapToFrame, snapToNearestFrame } from '../imaging/segment/frameSource.js';
import { createFrameImageLoader, frameImageId } from '../imaging/segment/frameImageLoader.js';

test('the frame on screen is the one whose interval contains the time', () => {
    assert.equal(snapToFrame(10.0, 30).frameIndex, 300);
    assert.equal(snapToFrame(10.0 + 1 / 60, 30).frameIndex, 300);
    assert.equal(snapToFrame(10.0 + 1 / 30 - 1e-3, 30).frameIndex, 300);
    assert.equal(snapToFrame(10.0 + 1 / 30, 30).frameIndex, 301);
});

test('float noise just under a frame boundary does not drop to the previous frame', () => {
    // 0.1 * 30 is 3.0000000000000004 or 2.9999999999999996 depending on the path taken.
    assert.equal(snapToFrame(0.1 + 0.2, 10).frameIndex, 3);
});

test('a frame is stored under milliseconds, and read from the middle of its interval', () => {
    const snapped = snapToFrame(10.0, 25);
    assert.equal(snapped.timeMs, 10000);
    assert.equal(snapped.seekSeconds, (250 + 0.5) / 25);
    assert.equal(snapToFrame(1 / 3, 30).timeMs, Math.round((10 * 1000) / 30));
});

test('a paused playback time snaps to the nearest subsampled frame', () => {
    assert.equal(snapToNearestFrame(10.49, 1, 20).frameIndex, 10);
    assert.equal(snapToNearestFrame(10.5, 1, 20).frameIndex, 11);
    assert.equal(snapToNearestFrame(99, 1, 20).frameIndex, 19, 'never seek beyond the last frame');
});

test('no frame rate means no frame', () => {
    for (const fps of [0, -1, NaN, undefined]) {
        assert.throws(() => snapToFrame(1, fps), /frame rate/);
    }
    assert.throws(() => snapToFrame(-1, 30), /video time/);
});

function fakeVideo({ width = 4, height = 2, seeks = [] } = {}) {
    const listeners = {};
    return {
        videoWidth: width,
        videoHeight: height,
        paused: false,
        pause() {
            this.paused = true;
        },
        addEventListener(name, callback) {
            listeners[name] = callback;
        },
        removeEventListener(name) {
            delete listeners[name];
        },
        set currentTime(value) {
            seeks.push(value);
            queueMicrotask(() => listeners.seeked?.());
        },
        get currentTime() {
            return seeks.at(-1) ?? 0;
        },
    };
}

const canvasOf = () => ({
    getContext: () => ({
        drawImage() {},
        getImageData: (_x, _y, w, h) => ({ data: new Uint8ClampedArray(w * h * 4).fill(7) }),
    }),
});

test('grab seeks to the middle of the frame, pauses, and returns its pixels', async () => {
    const seeks = [];
    const video = fakeVideo({ seeks });
    const source = createFrameSource({
        video,
        probe: { fps: 30, width: 4, height: 2 },
        createCanvas: canvasOf,
    });
    const frame = await source.grab(300);
    assert.deepEqual(seeks, [(300 + 0.5) / 30]);
    assert.equal(video.paused, true);
    assert.equal(frame.frameIndex, 300);
    assert.equal(frame.timeMs, 10000);
    assert.equal(frame.rgba.length, 4 * 2 * 4);
});

test('a decoded frame is reused instead of seeking and decoding it again', async () => {
    const seeks = [];
    const source = createFrameSource({
        video: fakeVideo({ seeks }),
        probe: { fps: 30, width: 4, height: 2, frameCount: 20 },
        createCanvas: canvasOf,
    });

    const first = await source.grab(4);
    const again = await source.grab(4);

    assert.equal(again, first);
    assert.deepEqual(seeks, [(4 + 0.5) / 30]);
});

test('a video of another size than its probe is refused', async () => {
    const source = createFrameSource({
        video: fakeVideo({ width: 640, height: 480 }),
        probe: { fps: 30, width: 1920, height: 1080 },
        createCanvas: canvasOf,
    });
    await assert.rejects(source.grab(0), /different grid/);
});

test('a seek that never completes is an error, not a hang', async () => {
    const video = fakeVideo();
    Object.defineProperty(video, 'currentTime', { set() {}, get: () => 0 });
    const source = createFrameSource({
        video,
        probe: { fps: 30, width: 4, height: 2 },
        createCanvas: canvasOf,
        timeoutMs: 5,
    });
    await assert.rejects(source.grab(1), /did not finish seeking/);
});

test('the frame loader answers from its registry and refuses an unknown id', async () => {
    const frames = new Map();
    const voxelManagerFactory = ({ scalarData }) => ({ getScalarData: () => scalarData });
    const load = createFrameImageLoader({ frames, voxelManagerFactory });
    const id = frameImageId('s1', 4200);
    frames.set(id, { rgba: new Uint8ClampedArray([10, 20, 30, 255, 40, 50, 60, 255]), width: 2, height: 1 });

    const image = await load(id).promise;
    assert.equal(image.columns, 2);
    assert.equal(image.numberOfComponents, 3);
    assert.equal(image.rowPixelSpacing, null, 'a video frame has no known spacing');
    await assert.rejects(load(frameImageId('s1', 1)).promise, /No frame is registered/);
});
