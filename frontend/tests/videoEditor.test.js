/**
 * The laparoscopy editor's frame handling, against the library's real rules.
 *
 * The defect this exists for could only be seen on the *second* frame a user visited, and
 * the whole suite only ever exercised the first.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { segmentation as realSegmentation } from '@cornerstonejs/tools';
import {
    createVideoEditor,
    nativePlaybackTransform,
    regionColorLUT,
} from '../imaging/video/editor.js';

test('adding a segmentation twice under one id throws', () => {
    // The upstream fact the editor has to be built around. `addSegmentations` was called
    // once per prepared frame with a fixed per-region id, so every frame after the first
    // threw out of `prepareFrame`, through `showFrame`, and into an unhandled rejection:
    // masks unpainted, navigation half-applied, nothing reported.
    const segmentationId = `ygg-test-${Math.random().toString(36).slice(2)}`;
    const input = {
        segmentationId,
        representation: { type: 'Labelmap', data: { imageIds: ['image:1'] } },
    };
    realSegmentation.addSegmentations([input]);
    assert.throws(() => realSegmentation.addSegmentations([input]), /already exists/);
    realSegmentation.removeSegmentation(segmentationId);
});

/** A `createVideoEditor` whose Cornerstone records what it was asked to do. */
async function editorOn(regionCodes, colorFor = () => null, options = {}) {
    const calls = {
        added: [],
        updated: [],
        representations: [],
        active: [],
        visibility: [],
        colors: [],
        stored: [],
    };
    const viewport = {
        setVideo: async () => {},
        pause() {},
        render() {},
        setFrameNumber() {},
        getActors: () => viewport.__actors,
        setActors: (actors) => {
            viewport.__actors = actors;
        },
        getImageData: () => ({
            imageData: { worldToIndex: ([x, y]) => [x, y, 0] },
        }),
        __actors: [],
    };
    const deps = {
        RenderingEngine: class {
            enableElement() {}
            getViewport() { return viewport; }
            resize() {}
            destroy() {}
        },
        coreEnums: { ViewportType: { VIDEO: 'video' } },
        // The real value, so a rename upstream fails here rather than leaving the
        // outline-to-mask listener registered for an event that never fires.
        toolsEnums: {
            MouseBindings: { Primary: 1 },
            Events: { ANNOTATION_COMPLETED: 'CORNERSTONE_TOOLS_ANNOTATION_COMPLETED' },
        },
        segmentation: {
            addSegmentations: (entries) => calls.added.push(...entries),
            updateSegmentations: (entries) => calls.updated.push(...entries),
            addSegmentationRepresentations: async (viewportId, entries) =>
                calls.representations.push(...entries),
            setActiveSegmentation: (viewportId, segmentationId) =>
                calls.active.push(segmentationId),
            triggerSegmentationDataModified() {},
            config: {
                visibility: {
                    setSegmentationRepresentationVisibility: (viewportId, { segmentationId }, visible) =>
                        calls.visibility.push([segmentationId, visible]),
                },
                color: {
                    setSegmentIndexColor: (viewportId, segmentationId, segmentIndex, color) =>
                        calls.colors.push([segmentationId, segmentIndex, color]),
                },
            },
        },
        SegmentationRepresentations: { Labelmap: 'Labelmap' },
        ToolGroupManager: {
            createToolGroup: () => ({ addTool() {}, addViewport() {}, setToolActive() {}, setToolPassive() {} }),
            destroyToolGroup() {},
        },
        addTool() {},
        tools: [],
        createAndCacheDerivedLabelmapImages: async ([frameImageId]) => [
            { imageId: `labelmap:${frameImageId}` },
        ],
        setBrushSizeForToolGroup() {},
        cache: { getImage: (id) => options.images?.get(id) ?? null },
        eventTarget: options.eventTarget,
        annotationState: options.annotationState,
    };
    const created = options.createdVideos ?? [];
    const element = {
        ownerDocument: {
            createElement: () => {
                const video = fakeVideoElement();
                created.push(video);
                return video;
            },
        },
    };
    const editor = await createVideoEditor(deps, {
        element,
        instanceId: 'test',
        videoImageId: 'yggvideo:http://x/v.mp4/frames/1',
        fps: 25,
        store: options.store ?? {
            width: 4,
            height: 3,
            peek: () => null,
            set: (timeMs, regionCode, mask, tool) =>
                calls.stored.push([timeMs, regionCode, tool]),
        },
        regionCodes,
        frameIdFor: (frameNumber) => `yggvideo:http://x/v.mp4/frames/${frameNumber}`,
        colorFor,
        playbackUrl: options.playbackUrl ?? null,
    });
    return { editor, calls, viewport };
}

/** A labelmap image whose voxels are a plain plane, as the cache hands one back. */
function labelmapImage(width, height) {
    const values = new Uint8Array(width * height);
    return { voxelManager: { getScalarData: () => values } };
}

test('a region is registered once and its layer grows a frame at a time', async () => {
    const { editor, calls } = await editorOn(['liver', 'gallbladder']);

    await editor.showFrame(1, 0, null);
    assert.equal(calls.added.length, 2, 'one segmentation per region, on the first frame');
    assert.equal(calls.updated.length, 0);

    await editor.showFrame(7, 240, 0);
    assert.equal(calls.added.length, 2, 'and never added again -- that would throw');
    assert.equal(calls.updated.length, 2);

    // Both frames' labelmaps, so Cornerstone can resolve the one belonging to whichever
    // frame is on screen -- a layer that knows one frame can only draw on that frame.
    assert.deepEqual(calls.updated[0].payload.representationData.Labelmap.imageIds, [
        'labelmap:yggvideo:http://x/v.mp4/frames/1',
        'labelmap:yggvideo:http://x/v.mp4/frames/7',
    ]);
});

test('the layer says which frame each of its labelmaps belongs to', async () => {
    // Without this the resolver compares the *labelmap* ids against the viewport's
    // current *frame* id -- `getReferencedImageIdForImageIndex` falls back to
    // `layer.imageIds` when `referencedImageIds` is absent -- matches nothing, and
    // `syncStackLabelmapActors` then removes every labelmap actor and adds none. The
    // stroke the brush had just painted disappeared on the render that followed it, with
    // no error, because dropping an actor is not one. See `editor.js`'s
    // `labelmapImageIds`, and `videoLabelmapSupport.test.js` for the upstream pin.
    const { editor, calls } = await editorOn(['liver']);

    await editor.showFrame(1, 0, null);
    assert.deepEqual(calls.added[0].representation.data, {
        imageIds: ['labelmap:yggvideo:http://x/v.mp4/frames/1'],
        referencedImageIds: ['yggvideo:http://x/v.mp4/frames/1'],
    });

    await editor.showFrame(7, 240, 0);
    assert.deepEqual(calls.updated[0].payload.representationData.Labelmap, {
        imageIds: [
            'labelmap:yggvideo:http://x/v.mp4/frames/1',
            'labelmap:yggvideo:http://x/v.mp4/frames/7',
        ],
        referencedImageIds: [
            'yggvideo:http://x/v.mp4/frames/1',
            'yggvideo:http://x/v.mp4/frames/7',
        ],
    });
});

test('each region draws in the colour its swatch shows', async () => {
    const colors = { liver: '#3498db', gallbladder: '#e74c3c' };
    const { editor, calls } = await editorOn(['liver', 'gallbladder'], (code) => colors[code]);
    await editor.showFrame(1, 0, null);

    assert.deepEqual(calls.representations[0].config.colorLUTOrIndex, [
        [0, 0, 0, 0],
        [0x34, 0x98, 0xdb, 255],
    ]);
    assert.deepEqual(calls.representations[1].config.colorLUTOrIndex, [
        [0, 0, 0, 0],
        [0xe7, 0x4c, 0x3c, 255],
    ]);
});

test('a colour that is not a hex colour leaves Cornerstone its default', () => {
    assert.equal(regionColorLUT('rebeccapurple'), null);
    assert.equal(regionColorLUT(undefined), null);
    assert.deepEqual(regionColorLUT('#ffffff'), [[0, 0, 0, 0], [255, 255, 255, 255]]);
});

test('a region added while the page is open does not re-register the ones already shown', async () => {
    // The reported failure: "The region type could not be added: Segmentation with id
    // ygg-video-patient-12-seg-fegato already exists" -- for a region the user was not
    // adding. `addRegion` used to clear the whole per-frame index so every frame would be
    // rebuilt with the new region in it, and the rebuild re-ran `addSegmentations` for the
    // existing regions too. It threw with the region already created on the server, which
    // is why clicking Add a second time appeared to work.
    const { editor, calls } = await editorOn(['fegato']);
    await editor.showFrame(1, 0, null);
    assert.equal(calls.added.length, 1);

    assert.equal(editor.addRegion('fegato'), false, 'a region already known is not re-added');
    assert.equal(editor.addRegion('cistifellea'), true);

    // What `pageControls` does next: re-show the current frame.
    await editor.showFrame(1, 0, 0);
    assert.equal(calls.added.length, 2, 'only the new region is registered');
    assert.deepEqual(
        calls.added.map((entry) => entry.segmentationId),
        ['ygg-video-test-seg-fegato', 'ygg-video-test-seg-cistifellea']
    );
    // The frame kept the buffer it already had for the region it already had: a fresh one
    // would have orphaned the strokes standing in the old one.
    assert.equal(calls.updated.length, 0);
});

test('a frame already prepared is not re-minted when nothing is missing', async () => {
    const { editor, calls } = await editorOn(['fegato']);
    await editor.showFrame(1, 0, null);
    await editor.showFrame(1, 0, 0);
    assert.equal(calls.added.length, 1);
    assert.equal(calls.updated.length, 0, 'the layer does not grow a duplicate of frame 1');
});

test('a video viewport declares the render mode a labelmap is mounted by', async () => {
    // `syncStackLabelmapActors` only takes the CanvasActor path -- the one a video can
    // draw -- when the viewport's default actor reports CPU_IMAGE. A legacy VideoViewport
    // has no default actor at all, so it took the vtk path and called `setInputData` on a
    // `CanvasMapper`, which does not have it. That is the reported
    // `getMapper(...).setInputData is not a function`, on mount and on every frame change.
    const { ActorRenderMode } = await import('@cornerstonejs/core');
    const { CPU_IMAGE_RENDER_MODE, declareCpuImageRendering } = await import(
        '../imaging/video/editor.js'
    );
    assert.equal(ActorRenderMode.CPU_IMAGE, CPU_IMAGE_RENDER_MODE);

    // Empty, as a video viewport is before any labelmap arrives.
    let actors = [];
    const viewport = { getDefaultActor: () => actors[0] };
    declareCpuImageRendering(viewport, ActorRenderMode);
    assert.equal(viewport.getDefaultActor().actorMapper.renderMode, ActorRenderMode.CPU_IMAGE);

    // And still, once the first labelmap has put a CanvasActor in front of it -- a
    // declaration that only covered the empty case would work for one frame and throw on
    // the next.
    const canvasActor = { getClassName: () => 'CanvasActor' };
    actors = [{ uid: 'seg', actor: canvasActor, referencedId: 'labelmap:1' }];
    const entry = viewport.getDefaultActor();
    assert.equal(entry.actorMapper.renderMode, ActorRenderMode.CPU_IMAGE);
    assert.equal(entry.actor, canvasActor, 'the real entry is passed through, not replaced');

    assert.throws(
        () => declareCpuImageRendering({}, { CPU_IMAGE: 'somethingElse' }),
        /selected by this exact value/
    );
});

test('a video viewport can drop a labelmap actor without a VTK renderer', async () => {
    // The reported crash: `Cannot read properties of undefined (reading 'removeActor')`,
    // on every frame change and after every brush stroke. `Viewport.addActor` guards the
    // renderer with `renderer?.addActor(actor)` and `Viewport._removeActor` does not
    // (`Viewport.js:279`) -- and a VideoViewport has no VTK renderer at all. Both callers
    // on the labelmap path skip that line when the viewport exposes `removeData`, so
    // implementing it is the whole fix.
    const { declareDataRemoval } = await import('../imaging/video/editor.js');
    const actors = [
        { uid: 'a', representationUID: 'seg-1|frame-1' },
        { uid: 'b', representationUID: 'seg-1|frame-2' },
    ];
    const viewport = {
        __actors: actors,
        getActors: () => viewport.__actors,
        setActors: (next) => {
            viewport.__actors = next;
        },
        // Deliberately present and deliberately fatal: this is what upstream reaches for
        // on the path `removeData` is there to avoid.
        getRenderer: () => undefined,
    };

    declareDataRemoval(viewport);
    assert.equal(typeof viewport.removeData, 'function');
    viewport.removeData('seg-1|frame-1');

    assert.deepEqual(
        viewport.getActors().map((entry) => entry.uid),
        ['b'],
        'the matching actor entry is gone and the rest are untouched'
    );
});

test('a viewport that implements removeData itself is left alone', async () => {
    // A future Cornerstone that fixes this properly must win over the shim.
    const { declareDataRemoval } = await import('../imaging/video/editor.js');
    const native = () => {};
    const viewport = { removeData: native, getActors: () => [], setActors() {} };

    assert.equal(declareDataRemoval(viewport).removeData, native);
});

test('the editor is installed on the viewport with both halves of the shim', async () => {
    const { editor, viewport } = await editorOn(['Liver']);

    assert.equal(typeof viewport.removeData, 'function');
    // And the add side it was always paired with.
    assert.equal(editor.viewport.getDefaultActor().actorMapper.renderMode, 'cpuImage');
});

test('the region the list says is selected is the one Cornerstone paints into', async () => {
    // Cornerstone marks the *most recently added* representation active
    // (`SegmentationStateManager.addDefaultSegmentationRepresentation`), and the
    // representations are registered in `regionCodes` order -- so on a fresh mount the
    // panel highlighted the first region, `editor.region` reported the first region, and
    // the brush painted into the last one until the user happened to click a chip.
    const { editor, calls } = await editorOn(['Liver', 'Gallbladder']);
    await editor.showFrame(1, 0, null);

    assert.equal(editor.region, 'Liver');
    assert.equal(
        calls.active.at(-1),
        editor.ids.segmentation('Liver'),
        'the last word on which segmentation is active must be the selected region'
    );
});

test('setActiveTool tells an unknown tool apart from a missing region', async () => {
    // Both used to answer `false`, so a toolbar button naming a tool that does not exist
    // reported "Pick a region before drawing on one" -- a true-sounding sentence with no
    // action behind it.
    const { editor } = await editorOn([]);
    assert.equal(editor.setActiveTool('nonesuch'), 'unknown');
    assert.equal(editor.setActiveTool('brush'), 'needs-region');
    assert.equal(editor.setActiveTool('pan'), 'ok');

    const { editor: withRegion } = await editorOn(['Liver']);
    assert.equal(withRegion.setActiveTool('brush'), 'ok');
    assert.equal(withRegion.activeTool, 'brush');
});

test('the armed tool is offered for every region, and the store decides', async () => {
    // The editor reads Cornerstone's buffer back for every region on every frame change,
    // so it cannot know which of them the reader touched -- only the store, which holds
    // the previous plane, can. It offers the armed tool and lets the store record it
    // where the mask actually changed; `videoMasks.test.js` pins that half.
    const images = new Map([
        ['labelmap:yggvideo:http://x/v.mp4/frames/1', labelmapImage(4, 3)],
    ]);
    const { editor, calls } = await editorOn(['Liver', 'Fat'], () => null, { images });
    await editor.showFrame(1, 0, null);
    editor.setActiveTool('brush');

    editor.flush(120);

    assert.deepEqual(
        calls.stored.filter(([timeMs]) => timeMs === 120),
        [
            [120, 'Liver', 'brush'],
            [120, 'Fat', 'brush'],
        ]
    );
});

test('a mask re-filed under another region keeps the tool that drew it', async () => {
    // It is the same mask under a new heading. The tool the reader happens to have armed
    // while re-filing it says nothing about who drew it, and `null` -- the record not
    // saying -- has to survive the move as `null` rather than being filled in.
    const images = new Map([
        ['labelmap:yggvideo:http://x/v.mp4/frames/1', labelmapImage(4, 3)],
    ]);
    const { editor, calls } = await editorOn(['Liver', 'Fat'], () => null, { images });
    await editor.showFrame(1, 0, null);
    editor.setActiveTool('brush');

    assert.equal(editor.moveRegionAt('Liver', 'Fat', 'polygon'), true);
    editor.flush(120);
    assert.deepEqual(
        calls.stored.filter(([timeMs]) => timeMs === 120),
        [
            [120, 'Liver', 'brush'],
            [120, 'Fat', 'polygon'],
        ]
    );

    // The override is consumed by that one pull, not left standing.
    calls.stored.length = 0;
    editor.flush(240);
    assert.deepEqual(calls.stored, [[240, 'Liver', 'brush'], [240, 'Fat', 'brush']]);
});

test('one region can be hidden without hiding the rest', async () => {
    const { editor, calls } = await editorOn(['Liver', 'Fat']);

    assert.equal(editor.setRegionVisible('Liver', false), true);
    assert.equal(editor.setRegionVisible('Spleen', false), false, 'a region it does not have');

    assert.deepEqual(calls.visibility, [[editor.ids.segmentation('Liver'), false]]);
});

test("a mask moves to another region's labelmap in place", async () => {
    const liver = labelmapImage(4, 3);
    const fat = labelmapImage(4, 3);
    const images = new Map([
        ['labelmap:yggvideo:http://x/v.mp4/frames/1', liver],
        // The second region on the same frame mints a second derived image; the fake
        // keys them by frame, so give the editor its own map entry per call order.
    ]);
    const { editor } = await editorOn(['Liver'], () => null, { images });
    await editor.showFrame(1, 0, null);
    liver.voxelManager.getScalarData()[5] = 1;

    // Only one region is prepared, so a move has nowhere to go and must say so rather
    // than clearing the source.
    assert.equal(editor.moveRegionAt('Liver', 'Fat'), false);
    assert.equal(liver.voxelManager.getScalarData()[5], 1);
    assert.equal(fat.voxelManager.getScalarData()[5], 0);

    // Clearing is the operation that does apply to a single region.
    assert.equal(editor.clearRegionAt('Liver'), true);
    assert.equal(liver.voxelManager.getScalarData()[5], 0);
});

test('a finished freehand outline is burned into the mask and then dropped', async () => {
    // `PlanarFreehandROI` is a measurement tool: it leaves an annotation and writes to no
    // mask. That is what makes it usable here -- it has no contour segmentation to demand
    // -- and it is why the annotation has to be consumed, or the page would show a curve
    // that no save carries.
    const image = labelmapImage(4, 3);
    const listeners = new Map();
    const removed = [];
    const { editor } = await editorOn(['Liver'], () => null, {
        images: new Map([['labelmap:yggvideo:http://x/v.mp4/frames/1', image]]),
        eventTarget: {
            addEventListener: (type, handler) => listeners.set(type, handler),
            removeEventListener: (type) => listeners.delete(type),
        },
        annotationState: { removeAnnotation: (uid) => removed.push(uid) },
    });
    await editor.showFrame(1, 0, null);
    editor.setActiveTool('polygon');

    listeners.get('CORNERSTONE_TOOLS_ANNOTATION_COMPLETED')({
        detail: {
            annotation: {
                annotationUID: 'outline-1',
                metadata: {
                    toolName: 'PlanarFreehandROI',
                    // **The `videoId:` prefix is what `VideoViewport` really records**, and
                    // this fixture used to omit it. That is why the burn shipped broken:
                    // the scope test was a `===` against the frame's imageId, which the
                    // prefix made false for every outline ever drawn -- so nothing was
                    // rasterised, nothing was removed, and the curve sat on screen looking
                    // like an annotation. See `video/metadata.js`'s `isSameVideoFrame`.
                    referencedImageId: 'videoId:yggvideo:http://x/v.mp4/frames/1',
                },
                data: {
                    contour: {
                        polyline: [
                            [0, 0, 0], [3, 0, 0], [3, 2, 0], [0, 2, 0],
                        ],
                    },
                },
            },
        },
    });

    assert.deepEqual(removed, ['outline-1'], 'the outline is not left on screen');
    assert.ok(
        image.voxelManager.getScalarData().some((value) => value === 1),
        'the outline was rasterised into the region it was drawn in'
    );
});

test('an outline from another tool is left entirely alone', async () => {
    const image = labelmapImage(4, 3);
    const listeners = new Map();
    const removed = [];
    const { editor } = await editorOn(['Liver'], () => null, {
        images: new Map([['labelmap:yggvideo:http://x/v.mp4/frames/1', image]]),
        eventTarget: { addEventListener: (type, handler) => listeners.set(type, handler) },
        annotationState: { removeAnnotation: (uid) => removed.push(uid) },
    });
    await editor.showFrame(1, 0, null);

    listeners.get('CORNERSTONE_TOOLS_ANNOTATION_COMPLETED')({
        detail: {
            annotation: {
                annotationUID: 'length-1',
                metadata: { toolName: 'Length' },
                data: { contour: { polyline: [[0, 0, 0], [3, 3, 0]] } },
            },
        },
    });

    assert.deepEqual(removed, []);
    assert.ok(image.voxelManager.getScalarData().every((value) => value === 0));
});


test('an outline drawn on another viewport is not burned into this video', async () => {
    // `ANNOTATION_COMPLETED` is announced on the library's global event target, so an
    // outline drawn anywhere on the page reaches every editor's handler. The annotation
    // records the image it was drawn against, and that is the scope test.
    const image = labelmapImage(4, 3);
    const listeners = new Map();
    const removed = [];
    const { editor } = await editorOn(['Liver'], () => null, {
        images: new Map([['labelmap:yggvideo:http://x/v.mp4/frames/1', image]]),
        eventTarget: { addEventListener: (type, handler) => listeners.set(type, handler) },
        annotationState: { removeAnnotation: (uid) => removed.push(uid) },
    });
    await editor.showFrame(1, 0, null);
    editor.setActiveTool('polygon');

    listeners.get('CORNERSTONE_TOOLS_ANNOTATION_COMPLETED')({
        detail: {
            annotation: {
                annotationUID: 'elsewhere',
                metadata: {
                    toolName: 'PlanarFreehandROI',
                    referencedImageId: 'wadouri:http://x/other/1',
                },
                data: {
                    contour: { polyline: [[0, 0, 0], [3, 0, 0], [3, 2, 0], [0, 2, 0]] },
                },
            },
        },
    });

    assert.deepEqual(removed, [], "another surface's annotation is left where it is");
    assert.ok(image.voxelManager.getScalarData().every((value) => value === 0));
});


test('a recoloured region repaints, rather than only moving its swatch', async () => {
    // `addSegmentationRepresentation` short-circuits on a `(segmentationId, type)` pair it
    // already holds, so re-registering -- which is what colours a *new* region -- does
    // nothing for one already on screen. The LUT entry has to be written directly.
    const { editor, calls } = await editorOn(['Liver']);

    assert.equal(editor.setRegionColor('Liver', '#3498db'), true);
    assert.deepEqual(calls.colors, [[editor.ids.segmentation('Liver'), 1, [52, 152, 219, 255]]]);

    // A colour that is not a colour leaves Cornerstone's, rather than painting it black.
    calls.colors.length = 0;
    assert.equal(editor.setRegionColor('Liver', 'rebeccapurple'), false);
    assert.equal(editor.setRegionColor('Spleen', '#3498db'), false, 'a region it does not have');
    assert.deepEqual(calls.colors, []);
});

test('a bare frame id is still recognised as this video', async () => {
    // Belt and braces. `getViewReferenceId` adds the prefix, but `createAnnotation` will
    // take a `referencedImageId` straight from its options when one is passed, and a
    // caller that does is handing over the imageId itself. Both spellings name the same
    // frame and both must burn.
    const image = labelmapImage(4, 3);
    const listeners = new Map();
    const removed = [];
    const { editor } = await editorOn(['Liver'], () => null, {
        images: new Map([['labelmap:yggvideo:http://x/v.mp4/frames/1', image]]),
        eventTarget: { addEventListener: (type, handler) => listeners.set(type, handler) },
        annotationState: { removeAnnotation: (uid) => removed.push(uid) },
    });
    await editor.showFrame(1, 0, null);
    editor.setActiveTool('polygon');

    listeners.get('CORNERSTONE_TOOLS_ANNOTATION_COMPLETED')({
        detail: {
            annotation: {
                annotationUID: 'outline-bare',
                metadata: {
                    toolName: 'PlanarFreehandROI',
                    referencedImageId: 'yggvideo:http://x/v.mp4/frames/1',
                },
                data: {
                    contour: { polyline: [[0, 0, 0], [3, 0, 0], [3, 2, 0], [0, 2, 0]] },
                },
            },
        },
    });

    assert.deepEqual(removed, ['outline-bare']);
    assert.ok(image.voxelManager.getScalarData().some((value) => value === 1));
});

/** A `<video>` with only the surface `setNativePlayback` touches. */
function fakeVideoElement() {
    return {
        style: {},
        hidden: true,
        autoplay: true,
        isConnected: false,
        currentTime: 0,
        plays: 0,
        pauses: 0,
        play() {
            this.plays += 1;
        },
        pause() {
            this.pauses += 1;
        },
    };
}

test('the overlay reproduces the canvas mapping, device pixels and all', () => {
    // `renderFrame` applies Cornerstone's matrix divided by the device pixel ratio, so a
    // CSS transform that does anything else puts the picture somewhere the canvas was not.
    assert.equal(nativePlaybackTransform([2, 0, 0, 2, 100, 50], 2), 'matrix(1, 0, 0, 1, 50, 25)');
    assert.equal(nativePlaybackTransform([2, 0, 0, 2, 100, 50]), 'matrix(2, 0, 0, 2, 100, 50)');
    // A ratio a browser did not state, or stated as nonsense, must not divide the picture
    // to nothing.
    assert.equal(nativePlaybackTransform([1, 0, 0, 1, 0, 0], 0), 'matrix(1, 0, 0, 1, 0, 0)');
    assert.equal(nativePlaybackTransform([1, 0, 0, 1, 0, 0], NaN), 'matrix(1, 0, 0, 1, 0, 0)');
});

test('playback outside annotation mode is the browser\'s, at the camera the reader left', async () => {
    const { editor, viewport } = await editorOn(['liver']);
    const video = fakeVideoElement();
    viewport.videoElement = video;
    viewport.videoWidth = 1920;
    viewport.videoHeight = 1080;
    viewport.getTransform = () => ({ getMatrix: () => [2, 0, 0, 2, 100, 50] });

    assert.equal(editor.setNativePlayback(true, 4200), video);
    assert.equal(video.hidden, false);
    // The two films share a clock, so the overlay starts on the frame the canvas holds.
    assert.equal(video.currentTime, 4.2);
    assert.equal(video.plays, 1);
    // Cornerstone's own element, adopted rather than duplicated -- and no longer able to
    // start the recording by itself the moment it enters the document.
    assert.equal(video.autoplay, false);
    assert.equal(video.style.position, 'absolute');
    // Tailwind's preflight (`img,video{height:auto;max-width:100%}`) resized the overlay
    // under it, and the UA `object-fit: contain` letterboxed the frame in the wrong box.
    assert.equal(video.style.maxWidth, 'none');
    assert.equal(video.style.maxHeight, 'none');
    assert.equal(video.style.objectFit, 'fill');
    assert.equal(video.style.transformOrigin, '0 0');
    assert.equal(video.style.width, '1920px');
    assert.equal(video.style.height, '1080px');
    assert.equal(video.style.transform, 'matrix(2, 0, 0, 2, 100, 50)');

    // Handing it back hides the overlay and stops the element. Where the *canvas* lands is
    // the caller's, because the frame a mask is filed against is the surface's own.
    assert.equal(editor.setNativePlayback(false), null);
    assert.equal(video.hidden, true);
    assert.equal(video.pauses, 1);
});

test('a resize mid-playback re-projects the overlay', async () => {
    const { editor, viewport } = await editorOn(['liver']);
    const video = fakeVideoElement();
    viewport.videoElement = video;
    viewport.videoWidth = 640;
    viewport.videoHeight = 480;
    viewport.getTransform = () => ({ getMatrix: () => [1, 0, 0, 1, 0, 0] });
    editor.setNativePlayback(true);

    // The container moved, so the camera's half-canvas did: an overlay left on the old
    // transform plays off-centre against the box it is playing in.
    viewport.getTransform = () => ({ getMatrix: () => [1, 0, 0, 1, 40, 10] });
    editor.resize();
    assert.equal(video.style.transform, 'matrix(1, 0, 0, 1, 40, 10)');

    // And a resize with the overlay away leaves it alone rather than un-hiding it.
    editor.setNativePlayback(false);
    viewport.getTransform = () => ({ getMatrix: () => [1, 0, 0, 1, 99, 99] });
    editor.resize();
    assert.equal(video.hidden, true);
    assert.equal(video.style.transform, 'matrix(1, 0, 0, 1, 40, 10)');
});

test('the film watched is the compressed one, not the subsampled one annotated', async () => {
    // The reported defect: the annotated track is one frame per source second (patient 15
    // probes at 1 fps / 187 frames), so pressing play stepped through stills -- "it plays
    // the cut frames". The page now hands down the compressed film of the same surgery,
    // and the overlay plays *that* while every mask stays filed against a subsampled frame.
    const created = [];
    const { editor, viewport } = await editorOn(['liver'], () => null, {
        playbackUrl: '/laparoscopy/api/file/48661/',
        createdVideos: created,
    });
    const cornerstoneVideo = fakeVideoElement();
    viewport.videoElement = cornerstoneVideo;
    viewport.videoWidth = 1920;
    viewport.videoHeight = 1080;
    viewport.getTransform = () => ({ getMatrix: () => [1, 0, 0, 1, 0, 0] });

    const overlay = editor.setNativePlayback(true, 42700);
    assert.equal(created.length, 1);
    assert.equal(overlay, created[0]);
    assert.equal(overlay.src, '/laparoscopy/api/file/48661/');
    // Muted, so a recording that carries an audio track does not start talking.
    assert.equal(overlay.muted, true);
    assert.equal(overlay.currentTime, 42.7);
    assert.equal(overlay.plays, 1);
    // Cornerstone's own element is left alone: it is still the decoder for the frame the
    // canvas is holding, and re-pointing its `src` would drop that frame.
    assert.equal(cornerstoneVideo.plays, 0);
    assert.equal(cornerstoneVideo.hidden, true);

    // Sized to the *annotated* film's rect, which is what the matrix maps into; the played
    // film is stretched into it by `object-fit: fill`.
    assert.equal(overlay.style.width, '1920px');
    assert.equal(overlay.style.objectFit, 'fill');

    // Re-projecting mid-run must not rewind the film to where the run started.
    overlay.currentTime = 51.3;
    editor.setNativePlayback(true, 42700);
    assert.equal(overlay.currentTime, 51.3);
    assert.equal(created.length, 1);
});
