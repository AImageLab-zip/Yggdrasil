/**
 * What the video bootstrap decides before Cornerstone is involved.
 *
 * Same contract the grid's and the photo stack's bootstraps are held to: read the DOM,
 * never throw into the page, and say out loud when it declines. The grid's first version
 * returned `null` from three places with no output, and a blank viewer that reports
 * nothing is indistinguishable from one that failed -- which is the reason these are
 * tests rather than a convention.
 */

import { strict as assert } from 'node:assert';
import test from 'node:test';

import {
    DATA_ELEMENT_ID,
    readCsrfToken,
    readVideoData,
    toolDecision,
} from '../imaging/video/bootstrap.js';
import {
    describeVideo,
    parseVideoImageId,
    videoImageId,
} from '../imaging/video/metadata.js';
import { applyWorkerMask, maskFromWorkerBytes } from '../imaging/video/magicSink.js';

function fakeDoc(text, id = DATA_ELEMENT_ID) {
    return {
        getElementById: (wanted) => (wanted === id ? { textContent: text } : null),
        cookie: '',
    };
}

test('a page with no payload is declined, not crashed', () => {
    assert.equal(readVideoData({ getElementById: () => null }), null);
});

test('malformed JSON is declined rather than thrown into the page', () => {
    assert.equal(readVideoData(fakeDoc('{not json')), null);
});

test('a payload missing the video URL is declined', () => {
    assert.equal(readVideoData(fakeDoc('{"patientId": 3}')), null);
});

test('a good payload is returned', () => {
    const data = readVideoData(
        fakeDoc('{"patientId": 3, "videoUrl": "/serve/9/v.mp4", "fps": 25}')
    );
    assert.equal(data.patientId, 3);
    assert.equal(data.fps, 25);
});

test('a drawing tool needs a region and a navigation tool does not', () => {
    assert.equal(toolDecision('pan', null).allowed, true);
    assert.equal(toolDecision('brush', null).allowed, false);
    assert.match(toolDecision('brush', null).reason, /pick a region/);
    assert.equal(toolDecision('brush', 'Liver').allowed, true);
    assert.equal(toolDecision('brush', 'Liver').tool, 'Brush');
});

test('an unknown toolbar key is refused rather than silently ignored', () => {
    assert.equal(toolDecision('teleport', 'Liver').allowed, false);
});

test('the CSRF token is read from the cookie every write carries', () => {
    assert.equal(readCsrfToken({ cookie: 'a=1; csrftoken=abc123; b=2' }), 'abc123');
    assert.equal(readCsrfToken({ cookie: '' }), '');
});

test('a video imageId carries a frame number, so frames do not share a labelmap', () => {
    // `origin` is explicit here for the same reason the photo stack's tests pass one:
    // under `node --test` there is no `location`, and a module that silently defaulted
    // would be untestable rather than merely awkward.
    const id = videoImageId({ url: 'https://x/v.mp4', frameNumber: 7, origin: 'https://x' });
    assert.equal(id, 'yggvideo:https://x/v.mp4/frames/7');
    assert.deepEqual(parseVideoImageId(id), { url: 'https://x/v.mp4', frameNumber: 7 });
});

test('frame zero is refused because Cornerstone counts frames from one', () => {
    assert.throws(
        () => videoImageId({ url: 'https://x/v.mp4', frameNumber: 0, origin: 'https://x' }),
        /1-based/
    );
});

test('an imageId with no frame is refused', () => {
    assert.throws(() => parseVideoImageId('yggvideo:https://x/v.mp4'), /names no frame/);
});

test('the metadata provider reports no pixel spacing', () => {
    // A laparoscope has no calibrated scale. Stating one would let LengthTool present
    // millimetres for a number that is pixels -- the claim MeasurementItem.is_calibrated
    // refuses in the database.
    const plane = describeVideo('imagePlaneModule', {
        width: 1920, height: 1080, fps: 25, numberOfFrames: 100,
    });
    assert.equal(plane.rows, 1080);
    assert.equal(plane.columns, 1920);
    assert.equal(plane.columnPixelSpacing, null);
    assert.equal(plane.rowPixelSpacing, null);
});

test('the metadata provider states the frame rate the server probed', () => {
    const cine = describeVideo('cineModule', { fps: 25, numberOfFrames: 100 });
    assert.equal(cine.cineRate, 25);
    assert.equal(cine.numberOfFrames, 100);
    assert.equal(cine.frameTime, 40);
});

test("the Magic Tool's mask is written as a mask, not traced", () => {
    // Decision #9 freezes the WebSocket worker; only the sink changes. The old annotator
    // traced the returned mask into a polygon because a polygon was all it could store,
    // and filtered out small components to make that polygon look reasonable -- throwing
    // away real, if small, predictions.
    const plane = new Uint8Array(4);
    applyWorkerMask({ plane, mask: maskFromWorkerBytes([0, 255, 0, 255], 2, 2) });
    assert.deepEqual([...plane], [0, 1, 0, 1]);
});

test('a worker mask of the wrong size is refused rather than resized', () => {
    assert.throws(
        () => applyWorkerMask({ plane: new Uint8Array(4), mask: new Uint8Array(9) }),
        /different frame size/
    );
});

test('subtracting is destructive, per decision #14', () => {
    const plane = Uint8Array.from([1, 1, 1, 1]);
    applyWorkerMask({ plane, mask: Uint8Array.from([0, 1, 1, 0]), mode: 'subtract' });
    assert.deepEqual([...plane], [1, 0, 0, 1]);
});
