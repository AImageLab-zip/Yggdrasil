/**
 * The one module under `video/` that touches Cornerstone.
 *
 * Same shape as `photos/stackViewport.js` and `grid/viewportManager.js`: Cornerstone is
 * *injected*, not imported, so everything else here stays `node --test`-able and only
 * this file needs a GPU. The Phase 3 record is the argument -- both defects that shipped
 * were in the region no data-path check looks at, so the way to shrink that region is to
 * keep it small.
 *
 * ## One segmentation per region, not one segment per region
 *
 * Cornerstone's labelmap is single-valued per voxel and regions here may overlap --
 * which is exactly what the layered NPZ export has always represented, and what
 * `annotations.services.video` stores. So each region gets its **own** segmentation with
 * its own segment 1. Sharing one segmentation with a segment index per region would
 * silently make the regions mutually exclusive: painting the gallbladder over the liver
 * would erase the liver, with nothing anywhere saying so.
 *
 * ## Labelmaps are per frame, and created on demand
 *
 * A labelmap is derived from a *referenced image*, and for a video that image is one
 * frame. A surgical recording is tens of thousands of frames, so derived images are
 * created for the frames the user actually annotates, as they arrive -- allocating one
 * per frame per region up front would be gigabytes of zeroes before the first stroke.
 *
 * A region's segmentation is therefore **registered once and extended**, never re-added.
 * `SegmentationStateManager.addSegmentation` *throws* on an id it already holds, and the
 * first version called `addSegmentations` again for every frame it prepared: the throw
 * came out of `prepareFrame`, through `showFrame`, and into an unhandled rejection, so
 * every frame after the first left the masks unpainted and the navigation half-applied.
 * That is what "the drawing doesn't appear" and "the skipping doesn't always work" were.
 * `updateSegmentations` is the supported way to grow the layer's `imageIds`, and growing
 * it is what lets Cornerstone resolve the right labelmap for the frame on screen --
 * `LabelmapImageReferenceResolver` matches a layer's **`referencedImageIds`** against the
 * viewport's current image id, so the layer has to carry the frame each of its labelmaps
 * belongs to. See {@link createVideoEditor}'s `labelmapImageIds` for what happens when it
 * does not.
 */

import { fillPolygon, polylineToPixels } from './polygonFill.js';
import { isSameVideoFrame } from './metadata.js';

/** Cornerstone ids for one instance of this surface. */
export function videoIds(instanceId) {
    return Object.freeze({
        renderingEngine: `ygg-video-${instanceId}`,
        viewport: `ygg-video-${instanceId}-0`,
        toolGroup: `ygg-video-${instanceId}-tools`,
        segmentation: (regionCode) => `ygg-video-${instanceId}-seg-${regionCode}`,
    });
}

/**
 * Which tool a toolbar button activates, and whether it needs a region selected.
 *
 * Data rather than a switch, so the "pick a region first" rule is a unit test rather
 * than a click.
 *
 * **The `tool` values are Cornerstone's `toolName` statics, and three of them are not
 * the class name minus `Tool`.** `RectangleScissorsTool.toolName` is `'RectangleScissor'`
 * -- singular -- `CircleScissorsTool.toolName` is `'CircleScissor'`, and
 * `PlanarFreehandContourSegmentationTool.toolName` keeps its `Tool` suffix. Guessing the
 * pattern is what shipped here: `toolGroup.addTool` warns `'RectangleScissors' is not
 * registered with the library` and *returns*, so the scissors and the polygon tool were
 * absent from the group and their toolbar buttons did nothing -- in the browser only,
 * because nothing in the suite read the real statics. `frontend/tests/videoToolNames.test.js`
 * now does, so a rename upstream fails the build instead.
 *
 * `Brush` and `Eraser` are the destructive pair decision #14 asks for; the scissors are
 * the fast way to clear a bad propagation. `polygon` is a freehand outline that is
 * rasterised into the mask and then discarded -- a user who prefers outlining still can,
 * and what is *stored* is still only the mask. See `polygonFill.js`.
 */
export const TOOL_PLAN = Object.freeze({
    pan: { tool: 'Pan', needsRegion: false },
    zoom: { tool: 'Zoom', needsRegion: false },
    brush: { tool: 'Brush', needsRegion: true },
    eraser: { tool: 'Eraser', needsRegion: true },
    'rect-scissors': { tool: 'RectangleScissor', needsRegion: true },
    'circle-scissors': { tool: 'CircleScissor', needsRegion: true },
    // **A plain freehand ROI, not the contour *segmentation* tool.** The latter refuses to
    // create an annotation at all unless a Contour segmentation is active, and this
    // surface stores labelmaps only -- see `polygonFill.js` for why adding one is the
    // wrong trade and what happens to the outline instead.
    polygon: { tool: 'PlanarFreehandROI', needsRegion: true },
    measure: { tool: 'Length', needsRegion: false },
    label: { tool: 'ArrowAnnotate', needsRegion: false },
});

/**
 * The tools bound on this surface, by the name Cornerstone knows them by.
 *
 * Derived from {@link TOOL_PLAN} rather than listed a second time: the two were separate
 * literals holding the same nine names, so a wrong name had to be corrected twice and a
 * tool could be registered with the group but reachable from no button, or the reverse.
 */
export const VIDEO_TOOL_NAMES = Object.freeze([
    ...new Set(Object.values(TOOL_PLAN).map((plan) => plan.tool)),
]);

/**
 * A `#rrggbb` region colour as the `[r, g, b, a]` a Cornerstone colour LUT wants.
 *
 * **Every region drew in the same colour without this.** A representation added with no
 * `colorLUTOrIndex` gets a clone of Cornerstone's default LUT, and each region here is
 * its own segmentation using its own segment 1 -- so every mask on screen came out the
 * default LUT's first colour, whatever swatch the region list showed beside its name.
 * "I cannot tell which region I am drawing in" is the accurate description of that.
 *
 * Index 0 is background and is transparent; index 1 is the region. Returns null for
 * anything that is not a hex colour, which leaves Cornerstone's default in place rather
 * than painting a mask black.
 *
 * @param {string} color e.g. `'#3498db'`.
 * @returns {number[][]|null}
 */
export function regionColorLUT(color) {
    const hex = /^#([0-9a-f]{6})$/i.exec(String(color ?? '').trim());
    if (!hex) {
        return null;
    }
    const value = Number.parseInt(hex[1], 16);
    return [
        [0, 0, 0, 0],
        [(value >> 16) & 255, (value >> 8) & 255, value & 255, 255],
    ];
}

/**
 * Cornerstone's `ActorRenderMode.CPU_IMAGE`, inlined -- see `grid/layout.js` for the rule.
 *
 * Checked by {@link declareCpuImageRendering} against the injected enum rather than
 * trusted, because a renamed value here would silently restore the crash below.
 */
export const CPU_IMAGE_RENDER_MODE = 'cpuImage';

/**
 * How solidly a region's mask covers the frame under it.
 *
 * High enough to read against saturated tissue, short of 1 so the anatomy the mask was
 * drawn around stays visible through it -- an opaque mask hides the edge the user is
 * tracing, which makes the next stroke worse.
 */
export const VIDEO_MASK_ALPHA = 0.85;

/** The labelmap style every region uses on the video viewport. */
export const VIDEO_MASK_STYLE = Object.freeze({
    renderFill: true,
    renderFillInactive: true,
    fillAlpha: VIDEO_MASK_ALPHA,
    fillAlphaInactive: VIDEO_MASK_ALPHA,
    renderOutline: true,
    renderOutlineInactive: true,
    outlineOpacity: 1,
    outlineOpacityInactive: 1,
});

/**
 * Make a legacy video viewport admit that it draws on a CPU canvas.
 *
 * **Without this a labelmap cannot be shown on a video at all in 5.8.2.** The relevant
 * code is `syncStackLabelmapActors`, which is what mounts a labelmap actor for a
 * non-volume viewport. Its branch is
 *
 * ```js
 * if (renderMode === 'image' && defaultActorRenderMode === ActorRenderMode.CPU_IMAGE)
 * ```
 *
 * where `defaultActorRenderMode` is `viewport.getDefaultActor()?.actorMapper?.renderMode`.
 * Take that branch and it calls `viewport.addImages` with no callback, `VideoViewport`
 * builds a `CanvasActor` from the derived image, and the mask is rasterised on the 2D
 * context the video is already drawn on -- the path the class exists for, and the one its
 * `setDerivedImage` hook on the update side is written against. Miss it and the same call
 * is made *with* a callback that does `imageActor.getMapper().setInputData(imageData)`;
 * `CanvasMapper` has `getInputData` and no `setInputData`, so it throws
 * `P.getMapper(...).setInputData is not a function` -- inside `addImages`, before
 * `setActors`, so no actor is registered and the next frame throws it again. That is the
 * reported stack, three times per mount and once per frame change after it.
 *
 * A legacy `VideoViewport` never takes the branch: the video is not an actor, so
 * `getActors()` is empty until a labelmap arrives and `getDefaultActor()` is `undefined`.
 * Nothing in core ever reports `CPU_IMAGE` outside the v2 planar viewport, so there is no
 * configuration that fixes this -- the viewport has to answer for itself.
 *
 * The answer is stated on the instance and is *stable*: once labelmap actors exist,
 * `getActors()[0]` is a `CanvasActor` entry, which still carries no `actorMapper`, and a
 * declaration that only covered the empty case would work for the first frame and fail on
 * every one after it. The real entry is passed through when there is one, so callers that
 * want the actor still get it.
 *
 * @param {object} viewport the video viewport.
 * @param {object} [actorRenderMode] Cornerstone's `ActorRenderMode`, for the check.
 * @returns {object} the viewport.
 */
export function declareCpuImageRendering(viewport, actorRenderMode) {
    if (actorRenderMode && actorRenderMode.CPU_IMAGE !== CPU_IMAGE_RENDER_MODE) {
        throw new Error(
            `ActorRenderMode.CPU_IMAGE is '${actorRenderMode.CPU_IMAGE}', not ` +
                `'${CPU_IMAGE_RENDER_MODE}'. A labelmap on a video viewport is selected by ` +
                'this exact value; a wrong one throws inside addImages instead of failing here.'
        );
    }
    const mapper = Object.freeze({ renderMode: CPU_IMAGE_RENDER_MODE });
    const native = viewport.getDefaultActor?.bind(viewport);
    viewport.getDefaultActor = () => {
        const entry = native?.();
        return entry ? { ...entry, actorMapper: entry.actorMapper ?? mapper } : { actorMapper: mapper };
    };
    return viewport;
}

/**
 * Let a video viewport drop a labelmap actor without going through a VTK renderer.
 *
 * **The other half of {@link declareCpuImageRendering}, and the reason a brush stroke
 * disappeared on mouse-up.** `Viewport.addActor` adds with `renderer?.addActor(actor)`,
 * optional chaining and all -- but `Viewport._removeActor` does
 *
 * ```js
 * const renderer = this.getRenderer();
 * renderer.removeActor(actorEntry.actor);   // Viewport.js:279, no guard
 * ```
 *
 * and a `VideoViewport` has no VTK renderer at all: it draws on a 2D canvas and its
 * labelmaps are `CanvasActor`s, so `getRenderer()` is `undefined` and that line is
 * `Cannot read properties of undefined (reading 'removeActor')`. Both callers on the
 * labelmap path hit it -- `removeLabelmapRepresentationFromViewport.js:15` and
 * `syncStackLabelmapActors.js:44` -- and every frame change invalidates an actor, so it
 * threw on every frame change and after every stroke.
 *
 * The damage is not limited to one console line. The throw escapes the
 * `requestAnimationFrame` callback of `SegmentationRenderingEngine._renderFlaggedSegmentations`
 * before its bookkeeping is cleared, and the stale actor is never removed -- so
 * `needsRemount` stays true, the next pass throws in the same place, and the mask the
 * brush had just painted is gone with no actor left to draw it.
 *
 * Upstream offers a supported way past it. Both call sites try
 * `removeLabelmapRepresentationData` first, which takes a data-driven path and returns
 * early **when the viewport exposes a `removeData` function**
 * (`removeLabelmapRepresentationData.js:11`) -- that is the hook the newer generic
 * viewports implement and the classic ones do not. Implementing it is the whole fix, and
 * it needs no private state: filter the entry out and hand the rest back through
 * `setActors`, which is exactly what `VideoViewport.addImages` itself does, and whose
 * `removeAllActors`/`addActor` path guards the renderer throughout.
 *
 * @param {object} viewport the video viewport.
 * @returns {object} the viewport.
 */
export function declareDataRemoval(viewport) {
    if (typeof viewport.removeData === 'function') {
        // A future Cornerstone that implements it properly must win over this shim.
        return viewport;
    }
    viewport.removeData = (representationUID) => {
        const kept = viewport
            .getActors()
            .filter((entry) => entry.representationUID !== representationUID);
        viewport.setActors(kept);
    };
    return viewport;
}

/**
 * The CSS transform that puts a `<video>` exactly where the canvas was drawing it.
 *
 * `VideoViewport.renderFrame` composes its camera into one affine matrix and applies it
 * as `ctx.transform(...m.map(v => v / devicePixelRatio))` before
 * `drawImage(video, 0, 0, videoWidth, videoHeight)` -- and `VideoViewport.resize` sizes
 * the backing store to `clientWidth`/`clientHeight`, so canvas pixels *are* CSS pixels
 * here and that same matrix is the CSS mapping. A `<video>` laid out at its own
 * `videoWidth`x`videoHeight` with `transform-origin: 0 0` and this matrix therefore lands
 * on the pixel the canvas was painting, at whatever zoom and pan the reader left the
 * camera in -- which is the whole reason the overlay can be swapped in mid-session
 * without the picture jumping. Reproducing the composition here instead would be a second
 * copy of Cornerstone's camera arithmetic, to drift out of step at the first upstream fix.
 *
 * @param {number[]} matrix Cornerstone's `[a, b, c, d, e, f]`, canvas order.
 * @param {number} [dpr] `window.devicePixelRatio`.
 * @returns {string} a CSS `matrix(...)`.
 */
export function nativePlaybackTransform(matrix, dpr = 1) {
    const scale = Number.isFinite(dpr) && dpr > 0 ? dpr : 1;
    return `matrix(${Array.from(matrix, (value) => value / scale).join(', ')})`;
}

/**
 * Create the editor.
 *
 * @param {object} deps every Cornerstone symbol this module uses, injected.
 * @param {object} options
 * @param {HTMLElement} options.element the viewport's host element.
 * @param {string} options.instanceId
 * @param {string} options.videoImageId the frame-1 imageId for this video.
 * @param {number} options.fps stated by the page, from the server's probe.
 * @param {ReturnType<import('./masks.js').createMaskStore>} options.store
 * @param {string[]} options.regionCodes every region the project defines.
 * @param {Function} options.frameIdFor `(frameNumber) => imageId`
 * @param {(regionCode: string) => string} [options.colorFor] the region's `#rrggbb`,
 *   so each mask draws in the colour its swatch promises. See {@link regionColorLUT}.
 */
export async function createVideoEditor(deps, options) {
    const {
        RenderingEngine,
        coreEnums,
        toolsEnums,
        segmentation,
        SegmentationRepresentations,
        ToolGroupManager,
        addTool,
        tools,
        createAndCacheDerivedLabelmapImages,
        setBrushSizeForToolGroup,
        cache,
        // The freehand outline is consumed rather than kept, so this module needs the
        // global event target it is announced on and the annotation store to drop it
        // from. See `burnOutline`.
        eventTarget,
        annotationState,
    } = deps;
    const {
        element, instanceId, videoImageId, store, regionCodes, frameIdFor,
        colorFor = () => null, playbackUrl = null,
    } = options;
    const ids = videoIds(instanceId);

    const engine = new RenderingEngine(ids.renderingEngine);
    engine.enableElement({
        viewportId: ids.viewport,
        type: coreEnums.ViewportType.VIDEO,
        element,
    });
    const viewport = declareDataRemoval(
        declareCpuImageRendering(engine.getViewport(ids.viewport), deps.ActorRenderMode)
    );
    await viewport.setVideo(videoImageId, 1);
    // Paused on arrival. Annotating a moving picture is annotating whichever frame the
    // compositor happened to show, which is not a decision the user made.
    viewport.pause();

    for (const tool of tools) {
        addTool(tool);
    }
    const toolGroup = ToolGroupManager.createToolGroup(ids.toolGroup);
    for (const name of VIDEO_TOOL_NAMES) {
        toolGroup.addTool(name);
    }
    toolGroup.addViewport(ids.viewport, ids.renderingEngine);

    /** Which frames already have derived labelmap images, per region. */
    const prepared = new Map();
    /**
     * Per region, the labelmap images minted so far and the frames they belong to --
     * two parallel arrays, oldest first.
     *
     * **The second array is the load-bearing one, and leaving it out is what made a
     * stroke vanish the moment it was drawn.** Cornerstone resolves which of a layer's
     * labelmap images belongs to the frame on screen in
     * `labelmapImageIdMapping.getReferencedImageIdForImageIndex`, which reads
     * `layer.referencedImageIds` and, when the layer does not state them, falls back to
     * `layer.imageIds` -- i.e. it compares the *labelmap* ids against the viewport's
     * current *frame* id and never matches. Resolution then falls through to
     * `layer.imageIds[frameIndex]`, indexing a list that has one entry per **annotated**
     * frame by a number that counts **every** frame of the recording; on frame 1 that is
     * accidentally right and on every other frame it is `undefined`. With nothing
     * resolved, `syncStackLabelmapActors` treats every labelmap actor as stale, removes
     * it and adds none -- so the mask the brush had just painted was dropped on the
     * render that followed the stroke, silently, because dropping an actor is not an
     * error. Cornerstone reads these off the segmentation state and never off the image
     * cache, so a derived image knowing its own `referencedImageId` does not help: the
     * layer has to say so.
     */
    const labelmapImageIds = new Map();
    let currentFrame = 1;
    let activeRegion = regionCodes[0] ?? null;
    /**
     * Which toolbar key is armed, so a mask can record what drew it.
     *
     * The record is one labelmap per (region, frame) and carries no stroke history, so
     * "the tool that made this" is the last one to touch the plane -- which is what the
     * annotation list shows and all it can honestly claim.
     */
    let activeToolKey = null;
    /**
     * Attribution that is not the armed tool, for the next {@link pull} to use.
     *
     * A mask moved between region types keeps the tool that drew it -- it is the same
     * mask -- and the tool the reader happens to have armed while re-filing it says
     * nothing. Consumed once, because it describes one write.
     *
     * @type {Map<string, string|null>}
     */
    const toolOverrides = new Map();

    /**
     * The labelmaps for one frame, minting only the ones that are missing.
     *
     * **Per region, not per frame.** The obvious shape -- return early when the frame is
     * known, rebuild everything when it is not -- cannot survive a region added while the
     * page is open: the new region needs a buffer on frames that already have one for
     * every *other* region, so `addRegion` had to invalidate the whole index, and the
     * rebuild then called `addSegmentations` a second time for regions Cornerstone was
     * already holding. That throws `Segmentation with id ... already exists`, which is
     * the reported "the region type could not be added" -- and it threw with the region
     * already created on the server, which is why doing it twice appeared to work.
     *
     * A segmentation is registered exactly once per region for the life of the surface,
     * because `labelmapImageIds` is now never cleared; a frame grows the layer it is
     * missing and nothing else is touched.
     */
    async function prepareFrame(frameNumber) {
        const byRegion = prepared.get(frameNumber) ?? new Map();
        prepared.set(frameNumber, byRegion);
        const missing = regionCodes.filter((regionCode) => !byRegion.has(regionCode));
        if (!missing.length) {
            return byRegion;
        }
        const frameImageId = frameIdFor(frameNumber);
        for (const regionCode of missing) {
            const [derived] = await createAndCacheDerivedLabelmapImages([frameImageId]);
            const labelmapImageId = derived.imageId ?? derived;
            const segmentationId = ids.segmentation(regionCode);

            const known = labelmapImageIds.get(regionCode);
            if (known) {
                // Registered already: grow the layer rather than re-adding the
                // segmentation, which throws. See this module's header.
                known.labelmaps.push(labelmapImageId);
                known.frames.push(frameImageId);
                segmentation.updateSegmentations([
                    {
                        segmentationId,
                        payload: {
                            representationData: {
                                [SegmentationRepresentations.Labelmap]: {
                                    imageIds: [...known.labelmaps],
                                    referencedImageIds: [...known.frames],
                                },
                            },
                        },
                    },
                ]);
            } else {
                labelmapImageIds.set(regionCode, {
                    labelmaps: [labelmapImageId],
                    frames: [frameImageId],
                });
                segmentation.addSegmentations([
                    {
                        segmentationId,
                        representation: {
                            type: SegmentationRepresentations.Labelmap,
                            data: {
                                imageIds: [labelmapImageId],
                                referencedImageIds: [frameImageId],
                            },
                        },
                    },
                ]);
            }
            byRegion.set(regionCode, labelmapImageId);
        }
        // Nothing to register when the surface mounted for playback only: no state was
        // read, so there are no regions and no masks to paint. The call is skipped rather
        // than made with an empty list, which is a request to show nothing.
        //
        // Repeating it whenever a layer grew is deliberate and cheap: the state manager
        // recognises a representation it already holds and re-announces it instead of
        // adding a second, which is what re-resolves the labelmap against the frame now on
        // screen -- and it is also what gives a region added mid-session its colour LUT.
        if (regionCodes.length) {
            await segmentation.addSegmentationRepresentations(
                ids.viewport,
                regionCodes.map((regionCode) => {
                    const colorLUT = regionColorLUT(colorFor(regionCode));
                    return {
                        segmentationId: ids.segmentation(regionCode),
                        type: SegmentationRepresentations.Labelmap,
                        ...(colorLUT ? { config: { colorLUTOrIndex: colorLUT } } : {}),
                    };
                })
            );
            // **The mask has to read as paint, not as a tint.**
            // Cornerstone's labelmap default is a 3px outline over a `fillAlpha` of 0.5
            // (`displayTools/Labelmap/labelmapConfig.js`), and every region here is a
            // *separate* segmentation, so all but the selected one render at the
            // `*Inactive` alphas, which are lower again. Over laparoscopy video --
            // saturated red-pink tissue under a moving specular highlight -- a
            // half-transparent wash of the region's colour is close to invisible, which
            // is what "make the line more opaque" was about: the stroke was landing, it
            // just could not be seen against what it was drawn on.
            //
            // {@link VIDEO_MASK_ALPHA} for both, deliberately equal: the annotator draws
            // one region at a time but is read as a whole, and a layer that fades when
            // the user picks a different chip changes what the record looks like without
            // changing the record. The grid's `SEGMENTATION_OPACITY` is *not* reused --
            // that 0.5 is tuned for ray accumulation in a volume render, a reason that
            // does not exist on a flat frame.
            for (const regionCode of regionCodes) {
                segmentation.config?.style?.setStyle?.(
                    {
                        viewportId: ids.viewport,
                        segmentationId: ids.segmentation(regionCode),
                        type: SegmentationRepresentations.Labelmap,
                    },
                    VIDEO_MASK_STYLE
                );
            }
            // **Re-assert the selection, because that call moved it.**
            // `SegmentationStateManager.addDefaultSegmentationRepresentation` pushes each
            // representation with `active: true` and then calls `_setActiveSegmentation`,
            // so whichever region is registered *last* becomes the active one. The list
            // above is in `regionCodes` order, so on a fresh mount the region list
            // highlighted the first region, `editor.region` reported the first region, and
            // the brush painted into the last one -- until the user happened to click a
            // chip, which is the only thing that ever called `selectRegion`.
            selectActiveSegmentation();
        }
        return byRegion;
    }

    /**
     * Point Cornerstone at the region the UI says is selected.
     *
     * Split out of `selectRegion` so `prepareFrame` can re-assert it without looking like
     * it is changing the user's selection.
     */
    function selectActiveSegmentation() {
        if (!activeRegion) {
            return;
        }
        segmentation.setActiveSegmentation?.(ids.viewport, ids.segmentation(activeRegion));
    }

    /** The labelmap plane for one region on the frame now on screen, or null. */
    function planeFor(regionCode) {
        const labelmapImageId = prepared.get(currentFrame)?.get(regionCode);
        const image = labelmapImageId ? cache.getImage(labelmapImageId) : null;
        return image?.voxelManager?.getScalarData?.() ?? null;
    }

    /**
     * Rasterise a finished freehand outline into the active region and drop the outline.
     *
     * `PlanarFreehandROI` is a measurement tool -- it leaves an annotation behind and
     * writes nothing to any mask. That is what makes it usable here (it has no contour
     * segmentation to demand) and it is why the annotation has to be consumed: leaving it
     * would put a curve on screen that no save carries and no reload brings back.
     */
    function burnOutline(annotation) {
        if (annotation?.metadata?.toolName !== TOOL_PLAN.polygon.tool) {
            return;
        }
        // **`ANNOTATION_COMPLETED` is announced on the library's *global* event target**,
        // not on this element, so an outline drawn anywhere reaches this handler. The
        // annotation records the image it was drawn against
        // (`AnnotationDisplayTool.createAnnotation` copies it out of the viewport's own
        // view reference), so that is the scope test; where it is absent, the tool being
        // armed on *this* editor is, since only this editor sets that.
        //
        // **Through `isSameVideoFrame`, never `===`.** A video's view reference is the
        // frame's imageId behind a `videoId:` prefix, so the direct comparison this
        // replaced was false for every outline ever drawn -- and a false answer here
        // neither burns the outline into the mask nor removes it, which is precisely "the
        // stroke draws but never saves". See `video/metadata.js`.
        const drawnOn = annotation.metadata.referencedImageId;
        const mine = drawnOn
            ? isSameVideoFrame(drawnOn, frameIdFor(currentFrame))
            : activeToolKey === 'polygon';
        const polyline = annotation.data?.contour?.polyline;
        const plane = mine && activeRegion ? planeFor(activeRegion) : null;
        if (!polyline?.length || !plane) {
            return;
        }
        const points = polylineToPixels(viewport, polyline);
        const { width, height } = store;
        const touched = points ? fillPolygon({ plane, width, height, points }) : 0;

        annotationState?.removeAnnotation?.(annotation.annotationUID);
        if (touched) {
            segmentation.triggerSegmentationDataModified?.(ids.segmentation(activeRegion));
        }
        viewport.render();
    }

    /** `ANNOTATION_COMPLETED` fires on the global event target, with the annotation. */
    function onAnnotationCompleted(event) {
        burnOutline(event?.detail?.annotation);
    }

    const completedEvent = toolsEnums.Events?.ANNOTATION_COMPLETED;
    if (completedEvent && eventTarget?.addEventListener) {
        eventTarget.addEventListener(completedEvent, onAnnotationCompleted);
    }

    /**
     * Write the store's masks for one instant into Cornerstone's buffers.
     *
     * Called after every frame change. There is no persistent per-frame labelmap state
     * inside the library, so assuming otherwise is how a mask ends up drawn on the wrong
     * picture.
     */
    function push(timeMs, byRegion) {
        for (const [regionCode, labelmapImageId] of byRegion) {
            const image = cache.getImage(labelmapImageId);
            if (!image) {
                continue;
            }
            const voxels = image.voxelManager.getScalarData();
            const stored = store.peek(timeMs, regionCode);
            if (stored) {
                voxels.set(stored);
            } else {
                voxels.fill(0);
            }
            segmentation.triggerSegmentationDataModified?.(ids.segmentation(regionCode));
        }
    }

    /**
     * Read Cornerstone's buffers back into the store.
     *
     * Called *before* every frame change and before every save. Miss it and the last
     * strokes on a frame are lost the moment the user scrubs away -- silently, because
     * the viewport shows the new frame either way.
     */
    function pull(timeMs, byRegion) {
        if (timeMs === null || !byRegion) {
            return;
        }
        for (const [regionCode, labelmapImageId] of byRegion) {
            const image = cache.getImage(labelmapImageId);
            if (!image) {
                continue;
            }
            // The armed tool is offered for every region, and the store records it only
            // where the mask actually changed -- it holds the previous plane, so it is the
            // only thing here that can tell. An override wins where one was left: a mask
            // re-filed under another region keeps the tool that drew it.
            const tool = toolOverrides.has(regionCode)
                ? toolOverrides.get(regionCode)
                : activeToolKey;
            store.set(timeMs, regionCode, image.voxelManager.getScalarData(), tool);
        }
        toolOverrides.clear();
    }

    // --- playback ---------------------------------------------------------------------

    /**
     * Whether the browser is compositing the recording itself.
     *
     * **Watching is not annotating, and it should not cost what annotating costs.**
     * `VideoViewport.play` drives a `requestAnimationFrame` loop over `renderFrame`, and
     * every pass of it re-blits the decoded frame into a 2D canvas, re-composites one
     * offscreen labelmap canvas per region on top, and fires `STACK_NEW_IMAGE` and
     * `IMAGE_RENDERED` -- the second of which is what
     * `imageRenderedEventDispatcher` turns into an annotation-render pass over every tool
     * in the group. At 1080p that is a per-frame budget a reader who is only looking gets
     * nothing for: the masks it composites belong to the frame the reader *left*, because
     * a labelmap is derived from one frame and playback does not prepare the frames it
     * passes through. So it is not merely slow, it is slow while painting the wrong thing.
     *
     * Outside annotation mode there is nothing to overlay, and the platform plays video
     * far better than a canvas loop can: the element is composited on the GPU with no
     * JavaScript per frame at all. {@link nativePlaybackTransform} is what makes the swap
     * invisible.
     *
     * **And the film it plays is not the film the canvas draws.** The annotated track is
     * the subsampled derivative -- one frame per source second -- so the canvas loop was
     * not only expensive, it was animating stills: for patient 15, 187 frames over 187
     * seconds. `playbackUrl` is the compressed film of the same surgery at 30 fps, and the
     * two share a clock, so watching one while every mask stays filed against a frame of
     * the other is exact rather than approximate. Without a `playbackUrl` the overlay
     * adopts Cornerstone's own `videoElement` instead -- one decoder for one film.
     */
    let nativePlayback = false;
    /** @type {object|null} The element the overlay plays, once built or adopted. */
    let overlay = null;

    /**
     * The `<video>` to lay over the canvas, in the document, once.
     *
     * With a `playbackUrl` this is a second element on the *other* film, which is a second
     * decoder and deliberately so: the alternative is re-pointing Cornerstone's `src` and
     * putting it back on every stop, which would drop the annotated track's decode state
     * and the frame on screen with it.
     *
     * Without one it is Cornerstone's own element, created in the `VideoViewport`
     * constructor and never appended -- upstream treats it purely as a decode source for
     * `drawImage`. `autoplay` is cleared either way: it is set on that element, and an
     * insertion is one of the moments the autoplay algorithm reconsiders, which would
     * start the recording on a page that never asked it to.
     */
    function nativeVideo() {
        if (overlay) {
            return overlay;
        }
        const video = playbackUrl
            ? Object.assign(element.ownerDocument.createElement('video'), {
                  src: playbackUrl,
                  // No audio track is expected and none is wanted; a surgical recording
                  // that carries one must not start talking because somebody pressed play
                  // on a viewer. `muted` is also what lets the element play unprompted.
                  muted: true,
                  preload: 'auto',
                  playsInline: true,
              })
            : viewport.videoElement;
        if (!video) {
            return null;
        }
        video.autoplay = false;
        video.hidden = true;
        Object.assign(video.style, {
            position: 'absolute',
            left: '0',
            top: '0',
            transformOrigin: '0 0',
            // **The site's CSS reset would otherwise resize the overlay under it.**
            // Tailwind's preflight ships `img,video{height:auto;max-width:100%}`
            // (`static/css/tailwind.css`), and `max-width` beats an inline `width`: the box
            // was clamped to the container's width while `projectNativeVideo` still laid it
            // out as `videoHeight` tall, and the UA stylesheet's `object-fit: contain` then
            // letterboxed the frame inside that wrong box -- a small, offset copy of the
            // picture beside a black band, over a canvas that was drawing it correctly.
            // `fill` on a box that is exactly `videoWidth`x`videoHeight` is the identity,
            // and says so rather than relying on the aspect ratios agreeing.
            maxWidth: 'none',
            maxHeight: 'none',
            objectFit: 'fill',
            // The reader is watching, not clicking: the tools stay bound to the canvas
            // under this, so a stray drag does not land on an overlay that is about to go.
            pointerEvents: 'none',
            // Over the canvas *and* over the annotation SVG layer, which is still showing
            // the frame being left behind.
            zIndex: '3',
        });
        // Cornerstone's own box, so `position: absolute` resolves against the same
        // containing block the canvas and the SVG layer use.
        (element.querySelector?.('div.viewport-element') ?? element).appendChild?.(video);
        overlay = video;
        return video;
    }

    /**
     * Lay the overlay over the canvas at the camera's current zoom and pan.
     *
     * Sized to the *annotated* film's pixels, not the played one's: the box has to be the
     * rect the canvas draws into for the matrix to land it there, and `object-fit: fill`
     * then stretches whatever film is playing into exactly that rect. So a compressed
     * derivative encoded at another resolution still lines up with the frame underneath it.
     */
    function projectNativeVideo(video) {
        video.style.width = `${viewport.videoWidth}px`;
        video.style.height = `${viewport.videoHeight}px`;
        video.style.transform = nativePlaybackTransform(
            viewport.getTransform().getMatrix(),
            globalThis.devicePixelRatio
        );
    }

    return {
        ids,
        viewport,
        toolGroup,

        get frameNumber() {
            return currentFrame;
        },

        get region() {
            return activeRegion;
        },

        /**
         * Take on a region type created after the surface mounted.
         *
         * The labelmaps are built per region in `prepareFrame`, so a region added while
         * the page is open is invisible to the editor until it is told -- and until then
         * every drawing tool refuses, because they all need a selected region and there
         * is none to select. That is exactly the state a project with no region types
         * starts in, which is where "Pick a region before drawing on one" was reported as
         * making no sense: it is a true statement with no action behind it.
         *
         * **Nothing is discarded.** An earlier version cleared `prepared` and
         * `labelmapImageIds` so that every frame would be rebuilt with the new region in
         * it, which also meant re-registering every *existing* region -- and
         * `addSegmentations` throws on a segmentationId Cornerstone already holds. That
         * was the reported failure. `prepareFrame` now fills in per region, so growing
         * the list is the whole of the change: the frames already prepared gain a buffer
         * for the new region the next time they are shown, and the ones that are not keep
         * theirs.
         *
         * The caller re-shows the current frame -- this returns without touching the
         * viewport, so a pull/push cycle is not run twice.
         *
         * @returns {boolean} whether the list changed.
         */
        addRegion(regionCode) {
            if (!regionCode || regionCodes.includes(regionCode)) {
                return false;
            }
            regionCodes.push(regionCode);
            return true;
        },

        selectRegion(regionCode) {
            activeRegion = regionCode;
            selectActiveSegmentation();
        },

        /**
         * Forget a region type deleted while the page is open.
         *
         * The segmentation is left registered: Cornerstone throws on re-adding an id it
         * already holds, and a project that re-creates a region under the same name would
         * then be unable to draw in it. Dropping it from `regionCodes` is enough here --
         * it stops being prepared and painted.
         *
         * **The store is not touched.** It belongs to the surface, which created it and
         * is what `updateRegionType` renames in; splitting that between two modules is how
         * a rename and a delete end up disagreeing about who cleans up.
         *
         * @returns {boolean} whether the list changed.
         */
        removeRegion(regionCode) {
            const at = regionCodes.indexOf(regionCode);
            if (at < 0) {
                return false;
            }
            regionCodes.splice(at, 1);
            segmentation.config?.visibility?.setSegmentationRepresentationVisibility?.(
                ids.viewport,
                { segmentationId: ids.segmentation(regionCode) },
                false
            );
            for (const byRegion of prepared.values()) {
                byRegion.delete(regionCode);
            }
            if (activeRegion === regionCode) {
                activeRegion = regionCodes[0] ?? null;
                selectActiveSegmentation();
            }
            viewport.render();
            return true;
        },

        /**
         * How wide the brush paints, in pixels.
         *
         * Through Cornerstone's helper rather than by writing the tool configuration:
         * the brush size is tool-group state, and the helper is also what invalidates
         * the cursor. Setting the configuration alone changes what is painted and
         * leaves the old circle drawn under the pointer, which reads as a brush that
         * ignores its own slider.
         */
        setBrushSize(size) {
            if (!Number.isFinite(size) || size <= 0) {
                return false;
            }
            setBrushSizeForToolGroup?.(ids.toolGroup, size);
            return true;
        },

        /**
         * Show or hide every region at once.
         *
         * Per representation, because that is the unit Cornerstone hides: this surface
         * registers one segmentation per region, so "hide all" is one call each rather
         * than a single flag. Reading is what this is for -- the masks are opaque enough
         * to hide the anatomy under them, and the alternative was to delete and redraw.
         */
        setRegionsVisible(visible) {
            for (const regionCode of regionCodes) {
                segmentation.config?.visibility?.setSegmentationRepresentationVisibility?.(
                    ids.viewport,
                    { segmentationId: ids.segmentation(regionCode) },
                    Boolean(visible)
                );
            }
            viewport.render();
            return regionCodes.length;
        },

        /**
         * Repaint one region's mask in a new colour.
         *
         * **Not through `addSegmentationRepresentations`.** Re-registering is what gives a
         * *new* region its colour LUT, but `addSegmentationRepresentation` short-circuits
         * on a `(segmentationId, type)` pair it already holds, so a recolour of a region
         * already on screen would change its swatch and leave its mask the old colour.
         * `config.color.setSegmentIndexColor` writes into the LUT entry in place and
         * announces it, which is the supported way to move a colour that is already
         * displayed. Segment 1, because this surface gives every region its own
         * segmentation and paints into that one index.
         */
        setRegionColor(regionCode, color) {
            const lut = regionColorLUT(color);
            if (!lut || !regionCodes.includes(regionCode)) {
                return false;
            }
            segmentation.config?.color?.setSegmentIndexColor?.(
                ids.viewport,
                ids.segmentation(regionCode),
                1,
                lut[1]
            );
            viewport.render();
            return true;
        },

        /**
         * Show or hide one region.
         *
         * The same call `setRegionsVisible` makes, for one representation. Deliberately
         * **not** persisted: whether a reader has a layer folded away while they work on
         * the one under it is not a fact about the study, and a stored flag would follow
         * them to another workstation and read as a missing annotation.
         */
        setRegionVisible(regionCode, visible) {
            if (!regionCodes.includes(regionCode)) {
                return false;
            }
            segmentation.config?.visibility?.setSegmentationRepresentationVisibility?.(
                ids.viewport,
                { segmentationId: ids.segmentation(regionCode) },
                Boolean(visible)
            );
            viewport.render();
            return true;
        },

        /**
         * Clear one region's mask on the frame now on screen.
         *
         * Through the live buffer rather than the store, so the change is on screen at
         * once and the next `pull` carries it into the store like any stroke would. A
         * cleared plane saves as an absent mask -- `build_mask_archive` writes only
         * non-empty ones.
         */
        clearRegionAt(regionCode) {
            const plane = planeFor(regionCode);
            if (!plane) {
                return false;
            }
            plane.fill(0);
            segmentation.triggerSegmentationDataModified?.(ids.segmentation(regionCode));
            viewport.render();
            return true;
        },

        /**
         * Move the frame's mask from one region to another, in place.
         *
         * The addressable unit of this record is a (region, frame) mask, so "switch the
         * region type of this annotation" is exactly this: copy the plane across and clear
         * the old one. The destination is **overwritten**, not merged -- merging would
         * make the operation irreversible in a way the row it is offered from does not
         * suggest.
         *
         * @param {string} fromCode
         * @param {string} toCode
         * @param {string|null} [tool] the tool the record says drew this mask, carried
         *   across because it is the same mask. Null when the record does not say, which
         *   must stay null rather than becoming whatever is armed right now.
         */
        moveRegionAt(fromCode, toCode, tool = null) {
            if (fromCode === toCode) {
                return false;
            }
            const source = planeFor(fromCode);
            const target = planeFor(toCode);
            if (!source || !target) {
                return false;
            }
            target.set(source);
            source.fill(0);
            toolOverrides.set(toCode, tool);
            segmentation.triggerSegmentationDataModified?.(ids.segmentation(fromCode));
            segmentation.triggerSegmentationDataModified?.(ids.segmentation(toCode));
            viewport.render();
            return true;
        },

        /**
         * Show one frame, carrying the current frame's edits into the store first.
         *
         * @param {number} frameNumber 1-based.
         * @param {number} timeMs the instant it corresponds to.
         * @param {number} currentTimeMs the instant being left.
         */
        async showFrame(frameNumber, timeMs, currentTimeMs) {
            pull(currentTimeMs, prepared.get(currentFrame));
            viewport.setFrameNumber(frameNumber);
            currentFrame = frameNumber;
            const byRegion = await prepareFrame(frameNumber);
            push(timeMs, byRegion);
            viewport.render();
        },

        /** Flush the viewport's buffers into the store. */
        flush(timeMs) {
            pull(timeMs, prepared.get(currentFrame));
        },

        /**
         * Play the recording the browser's way, or hand it back to the canvas.
         *
         * See {@link nativePlayback} for why watching does not go through `renderFrame`,
         * and which film it watches. Called again with `true` to re-project after the
         * camera or the container moved; calling it with `false` only stops the overlay --
         * the caller lands the canvas on a real frame, because which frame that is belongs
         * to the surface's own frame identity and not to Cornerstone's compositor.
         *
         * **`timeMs` is only obeyed on the first `true` of a run**, so a re-project mid-
         * playback does not seek the film back to where the run began. It is the instant
         * the canvas is showing: the two films share a clock, so handing the overlay the
         * same number in seconds starts it on the frame the reader was looking at.
         *
         * @param {boolean} on
         * @param {number} [timeMs] the instant to start from.
         * @returns {object|null} the element playing, for the caller to follow and to read
         *   the stopping instant off. Null when nothing can play.
         */
        setNativePlayback(on, timeMs = null) {
            const video = nativeVideo();
            if (!video) {
                return null;
            }
            if (!on) {
                nativePlayback = false;
                video.pause?.();
                video.hidden = true;
                return null;
            }
            const starting = !nativePlayback;
            projectNativeVideo(video);
            video.hidden = false;
            nativePlayback = true;
            if (starting && Number.isFinite(timeMs)) {
                video.currentTime = Math.max(0, timeMs / 1000);
            }
            video.play?.();
            return video;
        },

        get activeTool() {
            return activeToolKey;
        },

        /**
         * Arm one toolbar key.
         *
         * **The two failures are told apart.** Both used to return `false`, so a toolbar
         * button naming a tool that does not exist reported "Pick a region before drawing
         * on one" -- a true-sounding sentence with no action behind it, and the reported
         * confusion. `'unknown'` is a bug in the markup; `'needs-region'` is something the
         * reader can act on.
         *
         * `null` disarms instead, which is not the same as arming Pan: the surface opens
         * with no tool selected and goes back to that when annotation mode is switched
         * off, and a toolbar reporting "Pan" would be claiming a choice the reader did not
         * make.
         *
         * @param {string|null} key
         * @returns {'ok'|'unknown'|'needs-region'}
         */
        setActiveTool(key) {
            if (key === null) {
                // Disarm. Every tool passive, no primary binding, and nothing claiming to
                // be selected -- which is the state the surface opens in and returns to
                // when annotation mode is switched off. Passive rather than disabled
                // because a passive tool still *renders* what it is holding, and the
                // labelmaps have to stay on screen for a reader who is only looking.
                for (const name of VIDEO_TOOL_NAMES) {
                    toolGroup.setToolPassive(name);
                }
                activeToolKey = null;
                return 'ok';
            }
            const plan = TOOL_PLAN[key];
            if (!plan) {
                return 'unknown';
            }
            if (plan.needsRegion && !activeRegion) {
                return 'needs-region';
            }
            for (const name of VIDEO_TOOL_NAMES) {
                toolGroup.setToolPassive(name);
            }
            toolGroup.setToolActive(plan.tool, {
                bindings: [{ mouseButton: toolsEnums.MouseBindings.Primary }],
            });
            activeToolKey = key;
            return 'ok';
        },

        /**
         * Re-measure the canvas against its container.
         *
         * **Not optional on this surface.** The viewport is created while
         * `#video-annotate-viewport` still carries `d-none`, so `enableElement` sizes the
         * canvas to 0x0 and `renderFrame` draws a decoded frame into nothing. The page
         * removes the class a moment later and Cornerstone is never told, which presents
         * as a black box over a recording that loaded perfectly -- what
         * `[ygg-video] mounted` was reporting while nothing was on screen.
         * `pageControls.js` calls this from a ResizeObserver, the way the grid and the
         * photo stack already do.
         */
        resize() {
            engine.resize(true, false);
            viewport.render();
            // The overlay's transform is read off a camera whose half-canvas moved with
            // the container, so a resize mid-playback would leave the picture off-centre
            // against the box it is playing in.
            if (nativePlayback && viewport.videoElement) {
                projectNativeVideo(viewport.videoElement);
            }
        },

        destroy() {
            if (completedEvent) {
                eventTarget?.removeEventListener?.(completedEvent, onAnnotationCompleted);
            }
            ToolGroupManager.destroyToolGroup(ids.toolGroup);
            engine.destroy();
        },
    };
}
