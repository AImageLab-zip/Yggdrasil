/**
 * The arithmetic and the rules behind the laparoscopy page's controls.
 *
 * These lived in a `<script type="module">` in the patient template, where none of them
 * could be tested and most of them were never written: the frame bar, the timeline and
 * the region list were markup with nothing behind them. Phase 5's lesson applies to the
 * behaviour as well as the ids -- a control that is present and inert is worse than one
 * that is absent, and only a test tells the two apart without a browser.
 */

import { strict as assert } from 'node:assert';
import test from 'node:test';

import {
    FRAME_STEPS,
    formatTimestamp,
    frameTarget,
    keyAction,
    markerSegments,
    quadrantAt,
    trackPercent,
} from '../imaging/video/pageControls.js';

test('the timestamp is the one the template ships as its placeholder', () => {
    assert.equal(formatTimestamp(0), '00:00.000');
    assert.equal(formatTimestamp(1500), '00:01.500');
    assert.equal(formatTimestamp(61234), '01:01.234');
    assert.equal(formatTimestamp(3600000), '60:00.000');
    // Nothing to show is zero, not "NaN:NaN.NaN" in the corner of a viewer.
    assert.equal(formatTimestamp(undefined), '00:00.000');
    assert.equal(formatTimestamp(-5), '00:00.000');
});

test('the frame bar steps by the amounts its own buttons claim', () => {
    // The buttons are labelled -10s / -1s / +1s / +10s and carry those keys in their
    // titles, so this is the page's statement of intent rather than a choice made here.
    assert.deepEqual(FRAME_STEPS, {
        framePrev10: -10000,
        framePrev: -1000,
        frameNext: 1000,
        frameNext10: 10000,
    });
    assert.equal(frameTarget('frameNext', 5000, 60000), 6000);
    assert.equal(frameTarget('framePrev10', 5000, 60000), -5000);
    assert.equal(frameTarget('frameFirst', 5000, 60000), 0);
    assert.equal(frameTarget('frameLast', 5000, 60000), 60000);
});

test('"go to end" does nothing rather than jumping to the other end', () => {
    // A probe that did not state a frame count leaves the duration unknown. Returning 0
    // would send "last" to the first frame, which is the opposite of what was asked.
    assert.equal(frameTarget('frameLast', 5000, null), null);
    assert.equal(frameTarget('nonsense', 5000, 60000), null);
});

test('the keyboard says what the page says it says', () => {
    assert.deepEqual(keyAction({ key: 'ArrowRight' }), { kind: 'frame', action: 'frameNext' });
    assert.deepEqual(keyAction({ key: 'ArrowLeft', shiftKey: true }), {
        kind: 'frame',
        action: 'framePrev10',
    });
    assert.deepEqual(keyAction({ key: 'b' }), { kind: 'tool', tool: 'brush' });
    assert.deepEqual(keyAction({ key: 'E' }), { kind: 'tool', tool: 'eraser' });
    assert.deepEqual(keyAction({ key: 'p' }), { kind: 'tool', tool: 'polygon' });
    assert.deepEqual(keyAction({ key: 'h' }), { kind: 'tool', tool: 'pan' });
    assert.equal(keyAction({ key: 'z' }), null);
});

test('typing a patient note does not switch tools underneath the cursor', () => {
    for (const tagName of ['INPUT', 'TEXTAREA', 'SELECT']) {
        assert.equal(keyAction({ key: 'b', target: { tagName } }), null);
    }
    assert.equal(keyAction({ key: 'b', target: { isContentEditable: true } }), null);
    // And a browser shortcut stays the browser's.
    assert.equal(keyAction({ key: 'b', ctrlKey: true }), null);
    assert.equal(keyAction({ key: 'ArrowRight', metaKey: true }), null);
    assert.equal(keyAction(null), null);
});

test('a marker starts a quadrant and the next one ends it', () => {
    // Derived rather than stored: otherwise moving one marker would have to rewrite its
    // neighbour, and a half-applied edit would leave a gap nothing owns.
    const types = new Map([
        [1, { name: 'RUQ', color: '#ff0000' }],
        [2, { name: 'LUQ', color: '#00ff00' }],
    ]);
    const segments = markerSegments(
        [
            { time_ms: 20000, quadrant_type_id: 2 },
            { time_ms: 5000, quadrant_type_id: 1 },
        ],
        60000,
        types
    );
    assert.deepEqual(segments, [
        { startMs: 5000, endMs: 20000, name: 'RUQ', color: '#ff0000' },
        { startMs: 20000, endMs: 60000, name: 'LUQ', color: '#00ff00' },
    ]);
});

test('a marker for a type this project no longer has is still drawn', () => {
    // Deleting a quadrant type must not make an existing marker vanish from the
    // timeline: the record says something happened at that instant, and a blank track
    // would report that nothing did.
    const segments = markerSegments([{ time_ms: 0, quadrant_type_id: 99 }], 1000, new Map());
    assert.equal(segments.length, 1);
    assert.equal(segments[0].name, '—');
});

test('a timeline with no known duration draws nothing rather than everything at zero', () => {
    assert.deepEqual(markerSegments([{ time_ms: 0, quadrant_type_id: 1 }], null, new Map()), []);
    assert.equal(trackPercent(500, null), 0);
});

test('the quadrant in force at an instant, including after the last marker', () => {
    const types = new Map([[1, { name: 'RUQ', color: '#f00' }]]);
    const segments = markerSegments([{ time_ms: 10000, quadrant_type_id: 1 }], 60000, types);
    assert.equal(quadrantAt(segments, 5000), null, 'nothing is in force before the first marker');
    assert.equal(quadrantAt(segments, 10000).name, 'RUQ');
    assert.equal(quadrantAt(segments, 59999).name, 'RUQ');
    assert.equal(quadrantAt([], 100), null);
});

test('the playhead stays on the track at either end', () => {
    assert.equal(trackPercent(0, 60000), 0);
    assert.equal(trackPercent(30000, 60000), 50);
    assert.equal(trackPercent(90000, 60000), 100);
    assert.equal(trackPercent(-1, 60000), 0);
});

// ---------------------------------------------------------------------------
// Revealing the viewport
// ---------------------------------------------------------------------------

/**
 * The smallest document `bindVideoControls` will accept.
 *
 * Every binding in that function tolerates a missing element by design, so a plan
 * holding only the two elements this test is about exercises the reveal path and
 * nothing else.
 */
function revealDoc({ clientWidth }) {
    const viewport = {
        classes: new Set(['d-none']),
        clientWidth,
        offsetParent: {},
        classList: {
            remove(name) {
                viewport.classes.delete(name);
            },
            add(name) {
                viewport.classes.add(name);
            },
        },
        addEventListener() {},
        ownerDocument: { defaultView: { addEventListener() {}, removeEventListener() {} } },
    };
    let placeholderRemoved = false;
    const placeholder = {
        remove() {
            placeholderRemoved = true;
        },
    };
    const doc = {
        getElementById: (id) =>
            ({ 'video-annotate-viewport': viewport, 'video-placeholder': placeholder })[id] ?? null,
        addEventListener() {},
    };
    return { doc, viewport, wasPlaceholderRemoved: () => placeholderRemoved };
}

function fakeSurface(resizes) {
    return {
        // `setActiveTool` because binding disarms the toolbar on the way in -- the page
        // must open with nothing armed. See `pageControls.js`'s `markTool(null)`.
        editor: { setActiveTool: () => 'ok' },
        canAnnotate: false,
        reason: 'not under test',
        regionTypes: [],
        fps: 25,
        frameCount: null,
        durationMs: null,
        timeMs: 0,
        dirty: false,
        patientId: 1,
        markDirty() {},
        resize: () => resizes.push('resize'),
        goToInstant: async () => 0,
        save: async () => ({ ok: true }),
    };
}

test('revealing the viewport re-measures it, or the canvas stays 0x0 and black', async () => {
    // The viewport is built while the element carries `d-none`, so `enableElement` sizes
    // its canvas to nothing. Removing the class does not tell Cornerstone, and the
    // surface then reports `mounted` over a black box -- which is exactly how the
    // laparoscopy annotator presented.
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const resizes = [];
    const { doc, viewport, wasPlaceholderRemoved } = revealDoc({ clientWidth: 960 });

    bindVideoControls({ surface: fakeSurface(resizes), doc });

    assert.equal(viewport.classes.has('d-none'), false, 'the viewport must be revealed');
    assert.equal(wasPlaceholderRemoved(), true);
    assert.deepEqual(resizes, ['resize'], 'and re-measured once it can be measured');
});

test('an unmeasurable viewport is not resized, and the observer is left to say when', async () => {
    // `resize()` against a 0-width container would fix the canvas to zero rather than
    // leave it alone, and the page can legitimately mount this surface inside a tab that
    // is not open yet.
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const resizes = [];
    const { doc } = revealDoc({ clientWidth: 0 });

    bindVideoControls({ surface: fakeSurface(resizes), doc });

    assert.deepEqual(resizes, []);
});


/** A list element good enough for `drawRegions`, plus the document it needs. */
function regionDoc(regionTypes, { withAdd = false } = {}) {
    const children = [];
    const list = {
        get textContent() {
            return '';
        },
        set textContent(value) {
            if (value === '') children.length = 0;
        },
        appendChild: (node) => children.push(node),
        addEventListener() {},
    };
    const add = withAdd ? { addEventListener() {} } : null;
    const created = [];
    const doc = {
        getElementById: (id) =>
            ({ 'region-list': list, 'add-region-btn': add })[id] ?? null,
        createElement: (tag) => {
            const classes = new Set();
            const custom = {};
            const node = {
                tag,
                // `setProperty` because the chip hands the stylesheet the two colours it
                // cannot know -- the type's own, and a foreground readable on it.
                style: {
                    setProperty(name, value) {
                        custom[name] = value;
                    },
                },
                __custom: custom,
                dataset: {},
                textContent: '',
                className: '',
                classList: {
                    toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
                    contains: (name) => classes.has(name),
                },
                attributes: {},
                setAttribute(name, value) {
                    this.attributes[name] = String(value);
                },
                getAttribute(name) {
                    return this.attributes[name] ?? null;
                },
                append: () => {},
                appendChild(child) {
                    // Every child, not the last one: a region row is the region button
                    // *and* its hide/rename/delete controls, and a fake that kept only
                    // one of them silently reported the wrong node.
                    this.__children.push(child);
                    return child;
                },
                __children: [],
                /** The row's region button, by the attribute the binder selects on. */
                get __button() {
                    return this.__children.find((child) => child?.dataset?.region) ?? null;
                },
                /** The row's icon controls, keyed by their `data-*` name. */
                control(name) {
                    return this.__children.find((child) => child?.dataset?.[name]) ?? null;
                },
            };
            created.push(node);
            return node;
        },
        createTextNode: (text) => ({ text }),
        addEventListener() {},
        querySelector: () => null,
    };
    return { doc, children };
}

test('a project with no region types says so instead of showing nothing', async () => {
    // Every drawing tool needs a selected region, so an empty panel leaves the whole
    // toolbar answering "Pick a region before drawing on one" over nothing to pick --
    // a true sentence with no action behind it, which is how it was reported.
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, children } = regionDoc([], { withAdd: true });

    bindVideoControls({ surface: { ...fakeSurface([]), canAnnotate: true, reason: '', regionTypes: [] }, doc });

    assert.equal(children.length, 1);
    assert.equal(children[0].dataset.regionsEmpty, 'true');
    assert.match(children[0].textContent, /no region types yet/);
    // With the Add button present the sentence names the action; without it, it names
    // who can take it.
    assert.match(children[0].textContent, /Add one to start drawing/);
});

test('without the Add button the empty panel says who can fix it', async () => {
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, children } = regionDoc([], { withAdd: false });

    bindVideoControls({ surface: { ...fakeSurface([]), canAnnotate: true, reason: '', regionTypes: [] }, doc });

    assert.match(children[0].textContent, /An annotator can add one/);
});

test('a project with region types lists them, one button each', async () => {
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, children } = regionDoc([]);
    const surface = {
        ...fakeSurface([]),
        canAnnotate: true,
        reason: '',
        regionTypes: [
            { id: 1, name: 'Tool', color: '#3498db' },
            { id: 2, name: 'Liver', color: '#e74c3c' },
        ],
    };

    bindVideoControls({ surface, doc });

    assert.equal(children.length, 2);
    assert.equal(children.every((item) => item.dataset.regionsEmpty === undefined), true);
});

test('zoom goes through the camera, because a video viewport has no vtk one', async () => {
    // The defect: the three zoom buttons called
    // `viewport.setZoom?.(viewport.getZoom?.() * factor)`. Optional chaining reads as a
    // guard and is not one here -- both methods *exist*, inherited from `Viewport` -- and
    // the inherited `getZoom` goes through `getVtkActiveCamera()`. `VideoViewport` sets
    // `useCustomRenderingPipeline = true`, so the engine never makes it a vtk.js-driven
    // viewport and never adds a renderer for its id: `getVtkActiveCamera()` warns, returns
    // null, and `getZoom()` throws before `setZoom` is ever reached. Every press did
    // nothing but log, which is exactly how it was reported.
    const { VideoViewport } = await import('@cornerstonejs/core');
    assert.equal(VideoViewport.useCustomRenderingPipeline, true);
    assert.equal(Object.hasOwn(VideoViewport.prototype, 'setZoom'), false);
    assert.equal(Object.hasOwn(VideoViewport.prototype, 'getZoom'), false);
    // What it *does* implement itself, and therefore the seam the zoom has to use.
    assert.equal(Object.hasOwn(VideoViewport.prototype, 'setCamera'), true);
    assert.equal(Object.hasOwn(VideoViewport.prototype, 'getCamera'), true);

    const withoutVtkRenderer = Object.create(VideoViewport.prototype);
    withoutVtkRenderer.getRenderingEngine = () => ({ hasBeenDestroyed: false });
    withoutVtkRenderer.initialCamera = { parallelScale: 100 };
    assert.throws(() => withoutVtkRenderer.getZoom(), TypeError);

    const { zoomBy } = await import('../imaging/video/pageControls.js');
    let applied = null;
    const viewport = {
        getCamera: () => ({ parallelScale: 200, focalPoint: [1, 2, 3] }),
        setCamera: (camera) => { applied = camera; },
    };

    // `parallelScale` is half the world height on screen, so it moves the *opposite* way
    // to magnification: zooming in divides it. Getting that backwards is a zoom-out button
    // that zooms in.
    assert.equal(zoomBy(viewport, 2), true);
    assert.equal(applied.parallelScale, 100);
    assert.deepEqual(applied.focalPoint, [1, 2, 3], 'the view centre is kept');

    assert.equal(zoomBy(viewport, 0.5), true);
    assert.equal(applied.parallelScale, 400);

    // A viewport with no usable camera is left alone rather than given a NaN.
    assert.equal(zoomBy({ getCamera: () => ({}) }, 2), false);
    assert.equal(zoomBy(viewport, 0), false);
});

test('the selected region is filled with its own colour, in a readable foreground', async () => {
    // "I can't really understand which region I'm currently drawing in": Bootstrap's
    // `.active` on an outline button is a faint grey fill, which is the difference between
    // selected and merely hovered. The selection is the region's own colour -- the same
    // colour its mask is painted in -- and says so to a screen reader as well.
    const { bindVideoControls, readableOn } = await import('../imaging/video/pageControls.js');
    const { doc, children } = regionDoc([]);
    const surface = {
        ...fakeSurface([]),
        canAnnotate: true,
        reason: '',
        regionTypes: [
            { id: 1, name: 'Liver', color: '#3498db' },
            { id: 2, name: 'Fat', color: '#f4d03f' },
        ],
    };
    surface.editor.region = 'Fat';

    bindVideoControls({ surface, doc });

    assert.equal(children.length, 2);
    const [liver, fat] = children;

    // The fill is the chip's, not the inner button's, so the actions sitting inside it
    // are on the same ground and inherit the same readable foreground.
    assert.equal(liver.classList.contains('is-active'), false);
    assert.equal(fat.classList.contains('is-active'), true);

    // Handed to the stylesheet as custom properties: they come from the database, and
    // `.ygg-type-chip.is-active` is what turns them into a filled pill.
    assert.equal(fat.__custom['--chip-color'], '#f4d03f');
    // Pale yellow: white-on-yellow is what would make a filled selection unreadable.
    assert.equal(fat.__custom['--chip-ink'], '#000');
    assert.equal(liver.__custom['--chip-color'], '#3498db');
    assert.equal(readableOn('#3498db'), '#fff', 'and a dark blue takes white');

    assert.equal(liver.__button.getAttribute('aria-pressed'), 'false');
    assert.equal(fat.__button.getAttribute('aria-pressed'), 'true');
});
