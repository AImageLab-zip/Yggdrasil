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
 */

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
 * The tools bound on this surface, by the name Cornerstone knows them by.
 *
 * `Brush` and `Eraser` are the destructive pair decision #14 asks for; the scissors are
 * the fast way to clear a bad propagation. `PlanarFreehandContourSegmentation` replaces
 * the old polygon tool -- a user who preferred outlining still can, and what is *stored*
 * is still only the mask.
 */
export const VIDEO_TOOL_NAMES = Object.freeze([
    'Pan',
    'Zoom',
    'Brush',
    'Eraser',
    'RectangleScissors',
    'CircleScissors',
    'PlanarFreehandContourSegmentation',
    'Length',
    'ArrowAnnotate',
]);

/**
 * Which tool a toolbar button activates, and whether it needs a region selected.
 *
 * Data rather than a switch, so the "pick a region first" rule is a unit test rather
 * than a click.
 */
export const TOOL_PLAN = Object.freeze({
    pan: { tool: 'Pan', needsRegion: false },
    zoom: { tool: 'Zoom', needsRegion: false },
    brush: { tool: 'Brush', needsRegion: true },
    eraser: { tool: 'Eraser', needsRegion: true },
    'rect-scissors': { tool: 'RectangleScissors', needsRegion: true },
    'circle-scissors': { tool: 'CircleScissors', needsRegion: true },
    polygon: { tool: 'PlanarFreehandContourSegmentation', needsRegion: true },
    measure: { tool: 'Length', needsRegion: false },
    label: { tool: 'ArrowAnnotate', needsRegion: false },
});

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
        cache,
    } = deps;
    const { element, instanceId, videoImageId, store, regionCodes, frameIdFor } = options;
    const ids = videoIds(instanceId);

    const engine = new RenderingEngine(ids.renderingEngine);
    engine.enableElement({
        viewportId: ids.viewport,
        type: coreEnums.ViewportType.VIDEO,
        element,
    });
    const viewport = engine.getViewport(ids.viewport);
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
    let currentFrame = 1;
    let activeRegion = regionCodes[0] ?? null;

    async function prepareFrame(frameNumber) {
        if (prepared.has(frameNumber)) {
            return prepared.get(frameNumber);
        }
        const frameImageId = frameIdFor(frameNumber);
        const byRegion = new Map();
        for (const regionCode of regionCodes) {
            const [labelmapImageId] = await createAndCacheDerivedLabelmapImages([
                frameImageId,
            ]);
            const segmentationId = ids.segmentation(regionCode);
            segmentation.addSegmentations([
                {
                    segmentationId,
                    representation: {
                        type: SegmentationRepresentations.Labelmap,
                        data: { imageIds: [labelmapImageId.imageId ?? labelmapImageId] },
                    },
                },
            ]);
            byRegion.set(regionCode, labelmapImageId.imageId ?? labelmapImageId);
        }
        await segmentation.addSegmentationRepresentations(ids.viewport, [
            ...regionCodes.map((regionCode) => ({
                segmentationId: ids.segmentation(regionCode),
                type: SegmentationRepresentations.Labelmap,
            })),
        ]);
        prepared.set(frameNumber, byRegion);
        return byRegion;
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
            store.set(timeMs, regionCode, image.voxelManager.getScalarData());
        }
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

        selectRegion(regionCode) {
            activeRegion = regionCode;
            if (regionCode) {
                segmentation.setActiveSegmentation?.(
                    ids.viewport,
                    ids.segmentation(regionCode)
                );
            }
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

        setActiveTool(key) {
            const plan = TOOL_PLAN[key];
            if (!plan) {
                return false;
            }
            if (plan.needsRegion && !activeRegion) {
                return false;
            }
            for (const name of VIDEO_TOOL_NAMES) {
                toolGroup.setToolPassive(name);
            }
            toolGroup.setToolActive(plan.tool, {
                bindings: [{ mouseButton: toolsEnums.MouseBindings.Primary }],
            });
            return true;
        },

        destroy() {
            ToolGroupManager.destroyToolGroup(ids.toolGroup);
            engine.destroy();
        },
    };
}
