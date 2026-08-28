/**
 * The tooth editor's state machine, without a browser.
 *
 * Everything here is a behaviour the old editor had and had a reason for, and the reasons
 * are all about what a clinician ends up believing:
 *
 * - **The autosave coalesces.** A drag emits one change per mouse-move; without the
 *   debounce and the single-flight guard, twenty requests race on the revision number and
 *   nineteen of them lose to the constraint that exists to catch a *stale* writer.
 * - **"Saved." is only shown when it is true.** The version guard is what stops a stale
 *   response from claiming success over a newer unsaved edit.
 * - **A 409 reloads rather than retries.** Retrying would overwrite whoever won.
 * - **A confirmed image refuses edits** and puts the stored shape back, rather than leaving
 *   a change on screen that the server is going to reject.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { MESSAGES, SAVE_DELAY_MS, createToothEditor } from '../imaging/photos/toothSegmentation.js';

const TOOL = 'ToothOutline';
const IMAGE_A = 'yggweb:/a.jpg';
const IMAGE_B = 'yggweb:/b.jpg';
const RECORDS = [
    { fileId: 11, imageId: IMAGE_A },
    { fileId: 12, imageId: IMAGE_B },
];
const RING = [
    [10, 10],
    [30, 10],
    [30, 30],
];

/** A clock the test drives, so the debounce is deterministic. */
function fakeTimers() {
    let next = 1;
    const pending = new Map();
    return {
        setTimeoutImpl(callback, delay) {
            const id = next++;
            pending.set(id, { callback, delay });
            return id;
        },
        clearTimeoutImpl(id) {
            pending.delete(id);
        },
        /** Fire every timer currently queued, once. */
        async flush() {
            const due = [...pending.entries()];
            pending.clear();
            for (const [, entry] of due) {
                await entry.callback();
            }
        },
        get size() {
            return pending.size;
        },
        delays: () => [...pending.values()].map((entry) => entry.delay),
    };
}

/** A stand-in for the stack handle and Cornerstone's annotation state. */
function fakeViewer() {
    const annotations = [];
    let handlers = {};
    return {
        annotations,
        handlers: () => handlers,
        stack: {
            addToothOutline({ imageId, label, worldPoints, toolName }) {
                const annotation = {
                    annotationUID: `uid-${annotations.length + 1}`,
                    data: { label, handles: { points: worldPoints.map((point) => [...point]) } },
                    metadata: { toolName, referencedImageId: imageId },
                };
                annotations.push(annotation);
                return annotation;
            },
            setSegmentationMode() {},
            /** Records the visibility calls, so a test can see what the switch did. */
            visibility: [],
            setAnnotationsVisible(visible, toolNames) {
                this.visibility.push({ visible, toolNames: [...toolNames] });
                for (const annotation of annotations) {
                    if (toolNames.includes(annotation.metadata.toolName)) {
                        annotation.isVisible = visible;
                    }
                }
            },
            resetCamera() {},
            frameImageRegion() {},
            imageBounds: () => null,
            readAnnotations: () => annotations,
        },
        cornerstone: {
            toolName: TOOL,
            splineType: 'KONVA_TENSION',
            worldToImage: (_imageId, world) => [world[0], world[1]],
            imageToWorld: (_imageId, point) => [point[0], point[1], 0],
            readAnnotations: () => annotations,
            removeAnnotation(uid) {
                const index = annotations.findIndex((entry) => entry.annotationUID === uid);
                if (index >= 0) {
                    annotations.splice(index, 1);
                }
            },
            onAnnotationChange(next) {
                handlers = next;
                return () => {
                    handlers = {};
                };
            },
        },
    };
}

/** A fetch that records every call and replies from a queue. */
function fakeFetch(replies) {
    const calls = [];
    const queue = [...replies];
    return {
        calls,
        impl: async (url, init) => {
            calls.push({ url, init, body: init?.body ? JSON.parse(init.body) : null });
            const reply = queue.length > 1 ? queue.shift() : queue[0];
            return {
                ok: reply.status === undefined || reply.status < 400,
                status: reply.status ?? 200,
                async json() {
                    return reply.body ?? {};
                },
                async text() {
                    return JSON.stringify(reply.body ?? {});
                },
            };
        },
    };
}

function makeEditor({
    replies = [{ body: { revision: 3, images: {}, confirmations: {} } }],
    canModify = true,
} = {}) {
    const viewer = fakeViewer();
    const timers = fakeTimers();
    const fetch = fakeFetch(replies);
    const reports = [];
    const editor = createToothEditor({
        stack: viewer.stack,
        plan: {},
        toolName: TOOL,
        endpoints: { state: '/state/', save: '/save/' },
        cornerstone: viewer.cornerstone,
        io: {
            fetchImpl: fetch.impl,
            csrfToken: () => 'token',
            setTimeoutImpl: timers.setTimeoutImpl,
            clearTimeoutImpl: timers.clearTimeoutImpl,
        },
        canModify,
        report: (type, message) => reports.push({ type, message }),
    });
    editor.setImages(RECORDS);
    return { editor, viewer, timers, fetch, reports };
}

describe('load', () => {
    it('reads the polygons, the confirmations and the revision', async () => {
        const { editor } = makeEditor({
            replies: [
                {
                    body: {
                        revision: 4,
                        images: { 11: { 36: [RING] } },
                        confirmations: { 11: true, 12: false },
                    },
                },
            ],
        });
        await editor.load();
        assert.equal(editor.state.revision, 4);
        assert.deepEqual(editor.state.teethByFile[11], { 36: [RING] });
        assert.equal(editor.state.confirmations[11], true);
        assert.equal(editor.state.confirmations[12], false);
    });

    it('a failure is an exception the caller reports, not a silent empty state', async () => {
        // Drawing on top of state we failed to read and then saving over it is the outcome
        // being prevented.
        const { editor } = makeEditor({ replies: [{ status: 500 }] });
        await assert.rejects(() => editor.load(), /HTTP 500/);
    });
});

describe('showImage', () => {
    it('draws the stored outlines for that image and nothing else', async () => {
        const { editor, viewer } = makeEditor({
            replies: [
                {
                    body: {
                        revision: 1,
                        images: { 11: { 36: [RING, RING] }, 12: { 11: [RING] } },
                        confirmations: {},
                    },
                },
            ],
        });
        await editor.load();
        await editor.showImage(11);

        assert.equal(viewer.annotations.length, 2);
        assert.ok(viewer.annotations.every((entry) => entry.metadata.referencedImageId === IMAGE_A));
        assert.deepEqual(
            viewer.annotations.map((entry) => entry.data.label),
            ['36', '36']
        );
    });

    it('replaces rather than accumulates when the image changes', async () => {
        const { editor, viewer } = makeEditor({
            replies: [
                {
                    body: {
                        revision: 1,
                        images: { 11: { 36: [RING] }, 12: { 11: [RING] } },
                        confirmations: {},
                    },
                },
            ],
        });
        await editor.load();
        await editor.showImage(11);
        await editor.showImage(12);

        assert.equal(viewer.annotations.length, 1, 'image A\'s outline was removed');
        assert.equal(viewer.annotations[0].metadata.referencedImageId, IMAGE_B);
    });
});

describe('the autosave', () => {
    it('coalesces a burst of edits into one request', async () => {
        const { editor, viewer, timers, fetch } = makeEditor({
            replies: [
                { body: { revision: 1, images: {}, confirmations: {} } },
                { body: { revision: 2, confirmations: {} } },
            ],
        });
        await editor.load();
        await editor.showImage(11);
        editor.selectTooth('36');

        const annotation = viewer.stack.addToothOutline({
            imageId: IMAGE_A,
            label: '36',
            worldPoints: RING.map(([x, y]) => [x, y, 0]),
            toolName: TOOL,
        });
        // Twenty mouse-moves.
        for (let step = 0; step < 20; step += 1) {
            annotation.data.handles.points[0] = [10 + step, 10, 0];
            viewer.handlers().onChange(annotation);
        }

        assert.equal(timers.size, 1, 'one pending save, not twenty');
        assert.deepEqual(timers.delays(), [SAVE_DELAY_MS]);

        const before = fetch.calls.length;
        await timers.flush();
        assert.equal(fetch.calls.length, before + 1);
    });

    it('quotes the revision it loaded and never touches confirmation', async () => {
        const { editor, viewer, timers, fetch } = makeEditor({
            replies: [
                { body: { revision: 5, images: {}, confirmations: {} } },
                { body: { revision: 6, confirmations: {} } },
            ],
        });
        await editor.load();
        await editor.showImage(11);
        editor.selectTooth('36');

        const annotation = viewer.stack.addToothOutline({
            imageId: IMAGE_A,
            label: '36',
            worldPoints: RING.map(([x, y]) => [x, y, 0]),
            toolName: TOOL,
        });
        viewer.handlers().onChange(annotation);
        await timers.flush();

        const save = fetch.calls.at(-1);
        assert.equal(save.url, '/save/');
        assert.equal(save.body.expectedRevision, 5);
        assert.equal(save.body.images.length, 1);
        assert.equal(save.body.images[0].fileId, 11);
        assert.strictEqual(
            save.body.images[0].isConfirmed,
            null,
            'an autosave must not retract a confirmation it never mentioned'
        );
        assert.deepEqual(save.body.images[0].teeth['36'], [RING]);
        assert.equal(editor.state.revision, 6, 'the new revision is adopted');
    });

    it('a 409 reloads instead of retrying', async () => {
        const { editor, viewer, timers, fetch, reports } = makeEditor({
            replies: [
                { body: { revision: 1, images: {}, confirmations: {} } },
                { status: 409, body: { error: 'Segmentation changed elsewhere.', conflict: true } },
                { body: { revision: 9, images: { 11: { 11: [RING] } }, confirmations: {} } },
            ],
        });
        await editor.load();
        await editor.showImage(11);
        editor.selectTooth('36');

        const annotation = viewer.stack.addToothOutline({
            imageId: IMAGE_A,
            label: '36',
            worldPoints: RING.map(([x, y]) => [x, y, 0]),
            toolName: TOOL,
        });
        viewer.handlers().onChange(annotation);
        await timers.flush();

        // Retrying would overwrite whoever won the race the constraint just caught.
        const saves = fetch.calls.filter((call) => call.url === '/save/');
        assert.equal(saves.length, 1);
        assert.equal(editor.state.revision, 9, 'reloaded to the winner');
        assert.deepEqual(editor.state.teethByFile[11], { 11: [RING] });
        assert.ok(reports.some((entry) => entry.type === 'warning'));
    });

    it('a reader queues nothing at all', async () => {
        const { editor, viewer, timers, fetch } = makeEditor({ canModify: false });
        await editor.load();
        await editor.showImage(11);

        const annotation = viewer.stack.addToothOutline({
            imageId: IMAGE_A,
            label: '36',
            worldPoints: RING.map(([x, y]) => [x, y, 0]),
            toolName: TOOL,
        });
        viewer.handlers().onChange(annotation);

        assert.equal(timers.size, 0);
        assert.equal(fetch.calls.filter((call) => call.url === '/save/').length, 0);
    });
});

describe('a confirmed image', () => {
    it('refuses an edit and puts the stored shape back', async () => {
        const { editor, viewer, timers, reports } = makeEditor({
            replies: [
                {
                    body: {
                        revision: 2,
                        images: { 11: { 36: [RING] } },
                        confirmations: { 11: true },
                    },
                },
            ],
        });
        await editor.load();
        await editor.showImage(11);
        assert.equal(viewer.annotations.length, 1);

        const annotation = viewer.annotations[0];
        annotation.data.handles.points[0] = [999, 999, 0];
        viewer.handlers().onChange(annotation);

        assert.equal(timers.size, 0, 'nothing is queued for a confirmed image');
        assert.deepEqual(
            editor.state.teethByFile[11],
            { 36: [RING] },
            'the map is untouched'
        );
        assert.equal(viewer.annotations[0].data.handles.points[0][0], 10, 'redrawn from store');
        assert.ok(reports.some((entry) => entry.message === MESSAGES.confirmed));
    });

    it('reopening sends isConfirmed false immediately, not on a debounce', async () => {
        const { editor, timers, fetch } = makeEditor({
            replies: [
                { body: { revision: 2, images: { 11: { 36: [RING] } }, confirmations: { 11: true } } },
                { body: { revision: 3, confirmations: { 11: false } } },
            ],
        });
        await editor.load();
        await editor.showImage(11);
        await editor.toggleConfirmation();

        // An explicit act: the user is entitled to find out now whether it took.
        assert.equal(timers.size, 0);
        const save = fetch.calls.at(-1);
        assert.strictEqual(save.body.images[0].isConfirmed, false);
        assert.equal(editor.state.confirmations[11], false);
    });
});

describe('unassigned outlines', () => {
    it('are counted and reported rather than silently dropped', async () => {
        // Their polygons cannot be stored -- the server resolves the FDI code against the
        // seeded vocabulary -- so a shape drawn with no tooth selected must say so.
        const { editor, viewer } = makeEditor();
        await editor.load();
        await editor.showImage(11);

        viewer.stack.addToothOutline({
            imageId: IMAGE_A,
            label: '',
            worldPoints: RING.map(([x, y]) => [x, y, 0]),
            toolName: TOOL,
        });
        assert.equal(editor.unassignedCount(), 1);
    });

    it('a drawn outline takes the selected tooth', async () => {
        const { editor, viewer, timers } = makeEditor({
            replies: [
                { body: { revision: 1, images: {}, confirmations: {} } },
                { body: { revision: 2, confirmations: {} } },
            ],
        });
        await editor.load();
        await editor.showImage(11);
        editor.selectTooth('21');

        const annotation = viewer.stack.addToothOutline({
            imageId: IMAGE_A,
            label: '',
            worldPoints: RING.map(([x, y]) => [x, y, 0]),
            toolName: TOOL,
        });
        viewer.handlers().onChange(annotation);
        await timers.flush();

        assert.equal(annotation.data.label, '21');
        assert.deepEqual(Object.keys(editor.state.teethByFile[11]), ['21']);
        assert.equal(editor.unassignedCount(), 0);
    });
});

describe('undo', () => {
    it('a deleted outline comes back, geometry and position intact', async () => {
        // `ANNOTATION_REMOVED` fires *after* Cornerstone has dropped the annotation, so the
        // position cannot be looked up when the event arrives -- it has to have been
        // remembered. Without that the outline is deleted, saved, and not undoable.
        const { editor, viewer, timers } = makeEditor({
            replies: [
                {
                    body: {
                        revision: 1,
                        images: { 11: { 36: [RING, RING.map(([x, y]) => [x + 50, y])] } },
                        confirmations: {},
                    },
                },
                { body: { revision: 2, confirmations: {} } },
            ],
        });
        await editor.load();
        await editor.showImage(11);
        assert.equal(viewer.annotations.length, 2);

        const [, second] = viewer.annotations;
        const removed = { ...second, metadata: { ...second.metadata } };
        viewer.cornerstone.removeAnnotation(second.annotationUID);
        viewer.handlers().onRemoved(removed);
        await timers.flush();
        assert.equal(editor.state.teethByFile[11]['36'].length, 1);

        await editor.undo();
        assert.equal(editor.state.teethByFile[11]['36'].length, 2);
        assert.deepEqual(
            editor.state.teethByFile[11]['36'][1],
            RING.map(([x, y]) => [x + 50, y]),
            'restored at its original index, with its own geometry'
        );
    });

    it('a freshly drawn outline is removed by one undo, not un-clicked', async () => {
        const { editor, viewer, timers } = makeEditor({
            replies: [
                { body: { revision: 1, images: {}, confirmations: {} } },
                { body: { revision: 2, confirmations: {} } },
            ],
        });
        await editor.load();
        await editor.showImage(11);
        editor.selectTooth('36');

        const annotation = viewer.stack.addToothOutline({
            imageId: IMAGE_A,
            label: '',
            worldPoints: RING.map(([x, y]) => [x, y, 0]),
            toolName: TOOL,
        });
        viewer.handlers().onChange(annotation);
        await timers.flush();
        assert.deepEqual(Object.keys(editor.state.teethByFile[11]), ['36']);

        await editor.undo();
        assert.deepEqual(editor.state.teethByFile[11], {});
        assert.equal(viewer.annotations.length, 0, 'the viewer is redrawn from the map');
    });
});

describe('the Teeth switch', () => {
    const STORED = { 11: { 36: [RING] } };

    it('hides the outlines when it is off and shows them when it is on', async () => {
        // Symmetrical with the Measure switch, which hides its measurements when off.
        // Leaving outlines drawn under a switch reading "off" makes the switch look broken.
        const { editor, viewer } = makeEditor({
            replies: [{ body: { revision: 4, images: STORED, confirmations: {} } }],
        });
        await editor.load();
        await editor.showImage(11);

        editor.setMode(false);
        assert.equal(viewer.annotations.length, 1, 'still drawn, just not visible');
        assert.equal(viewer.annotations[0].isVisible, false);

        editor.setMode(true);
        assert.equal(viewer.annotations[0].isVisible, true);
    });

    it('re-applies the switch after a redraw', async () => {
        // A freshly added annotation is visible by default, so scrolling to another image
        // while the switch was off used to put that image's outlines back on screen.
        const { editor, viewer } = makeEditor({
            replies: [
                { body: { revision: 4, images: { 11: { 36: [RING] }, 12: { 11: [RING] } }, confirmations: {} } },
            ],
        });
        await editor.load();
        editor.setMode(false);

        await editor.showImage(12);
        assert.equal(viewer.annotations.length, 1);
        assert.equal(viewer.annotations[0].isVisible, false);
        assert.equal(
            viewer.stack.visibility.at(-1).toolNames.includes(TOOL),
            true,
            'and it is our tool that was hidden, not every annotation on the page'
        );
    });
});
