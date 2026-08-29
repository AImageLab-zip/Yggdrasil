/**
 * Which instant an annotation belongs to -- the same arithmetic on both sides.
 *
 * The fixtures are the ones `annotations/adapters/legacy_laparoscopy.py::frame_time_to_ms`
 * documents, because that function decided this for the converted corpus and a browser
 * that rounded differently would file a live save one millisecond off a converted one.
 * At 30 fps that is the same frame most of the time and a different one occasionally,
 * which is the worst kind of bug to be handed.
 */

import { strict as assert } from 'node:assert';
import test from 'node:test';

import {
    currentInstantMs,
    frameNumberForMs,
    msForFrameNumber,
    secondsToMs,
    snapToFrame,
} from '../imaging/video/frameIdentity.js';

test('seconds round to the nearest millisecond, never truncate', () => {
    // Truncating biases every annotation toward the previous frame: at 30 fps a boundary
    // lands on 33.3 ms, and int(0.0333 * 1000) is 33 while the next is 66 rather than
    // 67. Over a long operation that drift is a whole frame.
    assert.equal(secondsToMs(0.0333), 33);
    assert.equal(secondsToMs(0.0666), 67);
    assert.equal(secondsToMs(1.9999), 2000);
    assert.equal(secondsToMs(0), 0);
});

test('a non-finite or negative time is refused', () => {
    assert.throws(() => secondsToMs(Number.NaN), /finite/);
    assert.throws(() => secondsToMs(Infinity), /finite/);
    assert.throws(() => secondsToMs(-0.5), /negative/);
});

test('the instant comes from mediaTime when the browser offers it', () => {
    // currentTime is the playback clock and runs ahead of the composited frame, so
    // annotating from it while playing files the mask against the *next* frame.
    const video = { currentTime: 1.5 };
    assert.equal(currentInstantMs(video, { mediaTime: 1.4 }), 1400);
    assert.equal(currentInstantMs(video, undefined), 1500);
    assert.equal(currentInstantMs(video, {}), 1500);
});

test('two clicks on one paused frame snap to one instant', () => {
    // Otherwise they are two masks of the same picture, which the export would OR back
    // together -- correct by accident, and a second revision for no reason.
    assert.equal(snapToFrame(40, 30), snapToFrame(45, 30));
    assert.equal(snapToFrame(40, 30), 33);
});

test('with no stated frame rate every millisecond is its own instant', () => {
    // A browser cannot report a video's frame rate. Where the page does not state one,
    // this is exactly what the pre-Phase-10 record did.
    assert.equal(snapToFrame(40, 0), 40);
    assert.equal(snapToFrame(40.6, Number.NaN), 41);
});

test('frame numbers are 1-based and invert cleanly', () => {
    // Cornerstone counts frames from one, DICOM-style; the record counts milliseconds.
    assert.equal(frameNumberForMs(0, 30), 1);
    assert.equal(frameNumberForMs(33, 30), 2);
    assert.equal(msForFrameNumber(1, 30), 0);
    assert.equal(msForFrameNumber(2, 30), 33);
    for (const frame of [1, 2, 7, 250]) {
        assert.equal(frameNumberForMs(msForFrameNumber(frame, 25), 25), frame);
    }
});

test('a frame number needs a stated rate rather than a guess', () => {
    assert.throws(() => frameNumberForMs(100, 0), /stated frame rate/);
    assert.throws(() => msForFrameNumber(2, Number.NaN), /stated frame rate/);
});
