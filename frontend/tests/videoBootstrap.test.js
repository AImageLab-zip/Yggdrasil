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
    clampMs,
    frameSizeFor,
    readCsrfToken,
    readState,
    readVideoData,
} from '../imaging/video/bootstrap.js';
import {
    describeVideo,
    parseVideoImageId,
    videoImageId,
    isSameVideoFrame,
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

test('the CSRF token comes from the hidden input, because there is no cookie', () => {
    // `CSRF_USE_SESSIONS = True`, so this deployment sets no `csrftoken` cookie at all.
    // A cookie-only reader returns '' and every save is a bare 403 -- which is what the
    // grid's template comment records having happened there.
    const withInput = {
        querySelector: (selector) =>
            selector === 'input[name="csrfmiddlewaretoken"]' ? { value: 'from-input' } : null,
        cookie: 'csrftoken=from-cookie',
    };
    assert.equal(readCsrfToken(withInput), 'from-input');

    // The cookie remains the fallback: it is what a deployment without that setting has.
    assert.equal(
        readCsrfToken({ querySelector: () => null, cookie: 'a=1; csrftoken=abc123; b=2' }),
        'abc123'
    );
    assert.equal(readCsrfToken({ querySelector: () => null, cookie: '' }), '');
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

// --- which frame the masks describe ------------------------------------------------

test('the record states the frame size when there is a record', () => {
    // Taking it from the video would re-base every stored mask the first time a study
    // was re-encoded -- silently, and the export refuses to do exactly that.
    assert.deepEqual(
        frameSizeFor({ width: 1920, height: 1080 }, { width: 1920, height: 1080 }),
        { width: 1920, height: 1080 }
    );
});

test('a video with no record yet takes its size from the file', () => {
    assert.deepEqual(frameSizeFor({}, { width: 1280, height: 720 }), {
        width: 1280,
        height: 720,
    });
});

test('a record and a video that disagree on frame size refuse to be drawn', () => {
    // Reachable: the raw upload and the runner's compressed derivative are separate
    // registry rows with separate probes, and the page plays whichever ranks highest
    // that it can describe. Drawing the masks anyway puts every region somewhere it
    // was not, at full opacity, with nothing on screen looking wrong.
    const result = frameSizeFor(
        { width: 1920, height: 1080 },
        { width: 1280, height: 720 }
    );
    assert.match(result.refusal, /1920x1080/);
    assert.match(result.refusal, /1280x720/);
    assert.equal(result.width, undefined);
});

test('a page that states no size at all refuses rather than guessing', () => {
    assert.match(frameSizeFor({}, {}).refusal, /neither the record nor the page/);
});

// --- what the surface does when it cannot annotate ---------------------------------

test('an endpoint that fails is a reason, not an exception', async () => {
    // The surface plays the video either way, so the caller needs a sentence rather
    // than a rejection to handle. Saying "No video uploaded for this patient." over a
    // stored recording because an annotation endpoint was down is the defect this
    // shape exists to make impossible.
    const answered = await readState('/state/', async () => ({ ok: false, status: 500 }));
    assert.equal(answered.state, null);
    assert.match(answered.unavailable, /HTTP 500/);

    const unreachable = await readState('/state/', async () => {
        throw new Error('network down');
    });
    assert.equal(unreachable.state, null);
    assert.match(unreachable.unavailable, /network down/);
});

test('a state that reads gives no reason to degrade', async () => {
    const { state, unavailable } = await readState('/state/', async () => ({
        ok: true,
        json: async () => ({ revision: 4, frames: [], regionTypes: [] }),
    }));
    assert.equal(unavailable, '');
    assert.equal(state.revision, 4);
});

test('stepping past either end of the recording lands on it, not outside it', () => {
    // "back ten seconds" from the second frame and "forward" from the last are the two
    // the frame bar hits constantly.
    assert.equal(clampMs(-500, 20000), 0);
    assert.equal(clampMs(25000, 20000), 20000);
    assert.equal(clampMs(12345, 20000), 12345);
    // A probe that did not state a frame count clamps nothing rather than clamping
    // everything to zero.
    assert.equal(clampMs(99999, null), 99999);
    assert.equal(clampMs(Number.NaN, 20000), 0);
});

/** A mounted surface with a real mask store and a recording editor. */
async function mountedSurface({ frames = [], regionTypes = [] } = {}) {
    // `videoImageId` resolves the recording's URL against the document origin, which
    // `node --test` does not have.
    globalThis.location ??= { origin: 'http://x' };
    const { mountVideoAnnotator } = await import('../imaging/video/bootstrap.js');
    const flushes = [];
    let capturedStore = null;
    const doc = {
        getElementById: (id) =>
            ({
                videoAnnotateData: {
                    textContent: JSON.stringify({
                        patientId: 7,
                        videoUrl: 'http://x/v.mp4',
                        endpoint: '/state/',
                        fps: 25,
                        width: 4,
                        height: 3,
                        frameCount: 100,
                    }),
                },
                'video-annotate-viewport': {},
            })[id] ?? null,
    };
    const surface = await mountVideoAnnotator({
        doc,
        fetchImpl: async () => ({
            ok: true,
            status: 200,
            json: async () => ({ revision: 1, width: 4, height: 3, frames, regionTypes }),
        }),
        createEditor: async ({ store }) => {
            capturedStore = store;
            return {
                region: regionTypes[0]?.name ?? null,
                ids: { segmentation: (code) => `seg-${code}` },
                showFrame: async () => {},
                flush: (timeMs) => flushes.push(timeMs),
                addRegion: () => true,
                removeRegion: () => true,
                selectRegion() {},
                resize() {},
            };
        },
    });
    return { surface, flushes, store: capturedStore };
}

test('the annotation list joins the store to the region types', async () => {
    const { surface } = await mountedSurface({
        regionTypes: [
            { id: 1, name: 'Liver', color: '#3498db' },
            { id: 2, name: 'Fat', color: '#e74c3c' },
        ],
        frames: [
            { timeMs: 0, regions: { Liver: { rle: [5, 1, 6], tool: 'brush' } } },
            { timeMs: 1240, regions: { Fat: { rle: [5, 1, 6] } } },
        ],
    });

    assert.deepEqual(surface.annotations(), [
        { timeMs: 0, regionCode: 'Liver', color: '#3498db', tool: 'brush' },
        // No attribution in the record, and none invented for it.
        { timeMs: 1240, regionCode: 'Fat', color: '#e74c3c', tool: null },
    ]);
});

test('listing flushes first, or a stroke on the current frame is invisible', async () => {
    // The store is only written when a frame is left or a save runs, so without this the
    // list would not show the annotation the reader just drew until they navigated away.
    const { surface, flushes } = await mountedSurface({
        regionTypes: [{ id: 1, name: 'Liver', color: '#3498db' }],
    });

    flushes.length = 0;
    surface.annotations();
    assert.deepEqual(flushes, [0], 'the frame on screen was read back before listing');
});

test('a renamed region takes its masks and its attribution with it', async () => {
    // The archive is keyed by region code, so masks left under the old name would come
    // back as orphan codes on the next save.
    const { surface, store } = await mountedSurface({
        regionTypes: [{ id: 1, name: 'Liver', color: '#3498db' }],
        frames: [{ timeMs: 0, regions: { Liver: { rle: [5, 1, 6], tool: 'brush' } } }],
    });

    surface.updateRegionType('Liver', { name: 'Fegato', color: '#00ff00' });

    assert.deepEqual(surface.regionTypes, [{ id: 1, name: 'Fegato', color: '#00ff00' }]);
    assert.equal(store.peek(0, 'Liver'), null);
    assert.ok(store.peek(0, 'Fegato'));
    assert.equal(store.toolAt(0, 'Fegato'), 'brush');
});

test('a deleted region takes its masks out of the next save', async () => {
    const { surface, store } = await mountedSurface({
        regionTypes: [
            { id: 1, name: 'Liver', color: '#3498db' },
            { id: 2, name: 'Fat', color: '#e74c3c' },
        ],
        frames: [
            { timeMs: 0, regions: { Liver: { rle: [5, 1, 6] }, Fat: { rle: [5, 1, 6] } } },
        ],
    });

    surface.removeRegionType('Liver');

    assert.deepEqual(surface.regionTypes.map((type) => type.name), ['Fat']);
    // Leaving them would put them back on the next save under a code the project has no
    // name for.
    assert.deepEqual(store.regionsAt(0), ['Fat']);
});

test('a view reference names the frame it was drawn on, prefix and all', () => {
    // `VideoViewport.getViewReferenceId` returns `videoId:` + the frame's imageId, and
    // that string is what `AnnotationDisplayTool.createAnnotation` writes into
    // `metadata.referencedImageId`. The video editor's burn step compares against
    // `videoImageId(...)`, so without this the two never matched: the freehand outline was
    // neither rasterised into the mask nor removed from the store, which is a stroke that
    // draws and never saves.
    const frame = videoImageId({ url: 'http://x/v.mp4', frameNumber: 7 });

    assert.equal(isSameVideoFrame(`videoId:${frame}`, frame), true);
    // The bare form too: a caller that passes `referencedImageId` into `createAnnotation`
    // hands over the imageId itself.
    assert.equal(isSameVideoFrame(frame, frame), true);

    // A different frame of the same video is a different frame.
    assert.equal(
        isSameVideoFrame(
            `videoId:${videoImageId({ url: 'http://x/v.mp4', frameNumber: 8 })}`,
            frame
        ),
        false
    );
    // And an annotation from another surface entirely is not this video's.
    assert.equal(isSameVideoFrame('wadouri:http://x/other/1', frame), false);
    assert.equal(isSameVideoFrame(undefined, frame), false);
    assert.equal(isSameVideoFrame(`videoId:${frame}`, ''), false);
});
