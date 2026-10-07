/**
 * One annotation session: a single video frame, in a Cornerstone stack viewport, with the
 * project's labels as editable masks.
 *
 * The surface opens a session when the annotator enters Annotation Mode and closes it when
 * they leave (or move to another frame), so a session never outlives its frame: the stack,
 * the labelmaps and the undo history are all built for exactly one picture and thrown away
 * with it. That keeps "which frame is this mask for" from ever being a question the code
 * has to answer.
 *
 * Cornerstone is injected (see the entry for the concrete object), which is what keeps
 * `planes`, `labelmapStack`, `tools` and `saver` testable without a GPU; this file is the
 * glue between them and is exercised end to end in a browser.
 *
 * ## Edits and loads
 *
 * Every write to a labelmap fires `SEGMENTATION_DATA_MODIFIED`, including the ones this
 * session makes itself when it restores a saved mask. Treating those as edits would mark a
 * frame dirty the instant it opened, and autosave would write a revision for looking at it.
 * So loads run under a flag that the listener respects.
 */

import { decodePlane, encodePlane } from './planes.js';
import { createLabelmapStack } from './labelmapStack.js';
import { ALL_TOOL_NAMES, toolNameFor } from './tools.js';
import { frameImageId } from './frameImageLoader.js';

/**
 * A brush size is in *image* pixels, and a 1920-wide frame is shown ~900 px across, so a
 * fixed 12 drew a line about five screen pixels wide -- a mask that was there and could
 * not be seen. Scale with the frame instead.
 */
export function defaultBrushSize(width) {
    return Math.max(4, Math.round(width / 120));
}

export function maxBrushSize(width) {
    return Math.max(32, Math.round(width / 8));
}

/** Opaque enough that a stroke reads as a stroke over tissue, not a faint tint. */
const DEFAULT_OPACITY = 0.65;

/**
 * @param {object} options
 * @param {object} options.cornerstone the injected surface, see the entry.
 * @param {HTMLElement} options.element the viewport host.
 * @param {string} options.instanceId unique per mount of the surface.
 * @param {{rgba: Uint8ClampedArray, width: number, height: number, frameIndex: number,
 *   timeMs: number}} options.frame
 * @param {Map} options.frames the loader's registry; this session adds its frame.
 * @param {Map} options.records the metadata registry (width/height/no spacing).
 * @param {Array<{code: string, color: string, active: boolean}>} options.labels
 * @param {Function} options.onEdit called with the frame's `timeMs` after a user edit.
 * @param {object} [options.storedFrames] the manifest's `frames` for this file: `{timeMs: {...}}`.
 * @param {{tools: object, tool: object}} options.toolSet from `createSegmentTools` + names.
 */
export async function openSession({
    cornerstone,
    element,
    instanceId,
    frame,
    frames,
    records,
    labels,
    onEdit,
    api,
    fileId,
    storedFrames = {},
}) {
    const { RenderingEngine, coreEnums, toolsEnums, ToolGroupManager, segmentation, HistoryMemo } =
        cornerstone;

    const imageId = frameImageId(instanceId, frame.timeMs);
    frames.set(imageId, { rgba: frame.rgba, width: frame.width, height: frame.height });
    records.set(imageId, { width: frame.width, height: frame.height, pixelSpacingMm: null });

    const ids = {
        engine: `ygg-segment-${instanceId}`,
        viewport: `ygg-segment-${instanceId}-vp`,
        toolGroup: `ygg-segment-${instanceId}-tools`,
    };

    const engine = new RenderingEngine(ids.engine);
    engine.setViewports([
        { viewportId: ids.viewport, type: coreEnums.ViewportType.STACK, element },
    ]);
    const viewport = engine.getViewport(ids.viewport);
    await viewport.setStack([imageId], 0);
    viewport.render();
    const initialParallelScale = viewport.getCamera().parallelScale;

    function zoom(factor) {
        const camera = viewport.getCamera();
        const parallelScale = Math.max(
            initialParallelScale / 20,
            Math.min(initialParallelScale * 4, camera.parallelScale / factor)
        );
        viewport.setCamera({ ...camera, parallelScale });
        viewport.render();
    }

    function onWheel(event) {
        if (!event.ctrlKey) {
            return;
        }
        event.preventDefault();
        zoom(event.deltaY < 0 ? 1.2 : 1 / 1.2);
    }
    element.addEventListener('wheel', onWheel, { passive: false });

    // --- tools ---------------------------------------------------------------------
    try {
        ToolGroupManager.destroyToolGroup(ids.toolGroup);
    } catch {
        // None from a previous session.
    }
    const toolGroup = ToolGroupManager.createToolGroup(ids.toolGroup);
    for (const name of ALL_TOOL_NAMES) {
        toolGroup.addTool(name, cornerstone.toolConfiguration.get(name) ?? {});
    }
    toolGroup.addTool(cornerstone.Pan.toolName);
    toolGroup.addTool(cornerstone.Zoom.toolName);
    toolGroup.addViewport(ids.viewport, ids.engine);
    toolGroup.setToolActive(cornerstone.Pan.toolName, {
        bindings: [{ mouseButton: toolsEnums.MouseBindings.Auxiliary }],
    });
    toolGroup.setToolActive(cornerstone.Zoom.toolName, {
        bindings: [{ mouseButton: toolsEnums.MouseBindings.Secondary }],
    });
    cornerstone.enableTwoFingerNavigation?.(toolGroup, cornerstone.Zoom.toolName);

    // --- labels --------------------------------------------------------------------
    const stack = createLabelmapStack({
        cornerstone,
        viewportId: ids.viewport,
        imageId,
        prefix: ids.viewport,
    });

    let loading = true;
    const opacity = new Map();

    function onDataModified(event) {
        if (loading) {
            return;
        }
        if (stack.codeForSegmentation(event.detail?.segmentationId) !== null) {
            onEdit(frame.timeMs);
        }
    }
    cornerstone.eventTarget.addEventListener(
        toolsEnums.Events.SEGMENTATION_DATA_MODIFIED,
        onDataModified
    );

    async function mountLabel(label) {
        await stack.addLabel(label);
        // Every label must stay drawn while another is the active one. Only valid once the
        // viewport has at least one representation -- before that Cornerstone reads an
        // undefined list and throws -- so it follows the first mount rather than preceding it.
        segmentation.config.style.setRenderInactiveSegmentations(ids.viewport, true);
        opacity.set(label.code, DEFAULT_OPACITY);
        stack.setOpacity(label.code, DEFAULT_OPACITY);
    }

    // Every label is mounted, **retired ones too, hidden**. A save replaces a frame with
    // exactly the planes it sends, so a retired label left unmounted would have its pixels
    // dropped the next time anyone saved the frame -- and retiring a label must never
    // delete the work already drawn with it.
    for (const label of labels) {
        await mountLabel(label);
        if (!label.active) {
            stack.setVisible(label.code, false);
        }
    }

    // Restore the frame's saved mask, if any.
    // Asked for only when the manifest says it exists: a missing mask is the normal case, and
    // a 404 per frame opened would fill the console with what is not an error.
    const saved = String(frame.timeMs) in storedFrames ? await api.frame(fileId, frame.timeMs) : null;
    if (saved && saved.width === frame.width && saved.height === frame.height) {
        for (const [code, encoded] of Object.entries(saved.planes)) {
            if (stack.has(code)) {
                stack.writePlane(code, await decodePlane(encoded, saved.width, saved.height));
            }
        }
    } else if (saved) {
        // A mask drawn on another grid is not resampled: it would be a different picture's.
        throw new Error(
            `The stored mask is ${saved.width}x${saved.height}; this frame is ` +
                `${frame.width}x${frame.height}. It will not be loaded over the wrong grid.`
        );
    }
    loading = false;

    // --- tool selection ------------------------------------------------------------
    let toolId = 'brush';

    function activateTool() {
        for (const name of ALL_TOOL_NAMES) {
            toolGroup.setToolPassive(name);
        }
        toolGroup.setToolActive(toolNameFor(toolId), {
            bindings: [{ mouseButton: toolsEnums.MouseBindings.Primary }],
        });
    }
    cornerstone.setBrushSize(ids.toolGroup, defaultBrushSize(frame.width));
    HistoryMemo.DefaultHistoryMemo.size = HistoryMemo.DefaultHistoryMemo.size; // fresh history
    const first = labels.find((candidate) => candidate.active);
    if (first) {
        stack.setActive(first.code);
    }
    activateTool();

    const session = {
        frame: {
            frameIndex: frame.frameIndex,
            timeMs: frame.timeMs,
            width: frame.width,
            height: frame.height,
        },
        brushSize: { initial: defaultBrushSize(frame.width), max: maxBrushSize(frame.width) },
        labels: stack,

        setTool(id) {
            toolId = id;
            activateTool();
        },
        setBrushSize(size) {
            cornerstone.setBrushSize(ids.toolGroup, size);
        },
        zoomIn: () => zoom(1.2),
        zoomOut: () => zoom(1 / 1.2),
        resetView() {
            viewport.resetCamera();
            viewport.render();
        },
        selectLabel(code) {
            stack.setActive(code);
        },
        async addLabel(label) {
            loading = true;
            try {
                await mountLabel(label);
            } finally {
                loading = false;
            }
        },
        removeLabel(code) {
            stack.removeLabel(code);
        },
        setVisible: (code, visible) => stack.setVisible(code, visible),
        setOpacity(code, alpha) {
            opacity.set(code, alpha);
            stack.setOpacity(code, alpha);
        },
        opacityOf: (code) => opacity.get(code) ?? DEFAULT_OPACITY,
        setColor: (code, color) => stack.setColor(code, color),

        undo() {
            HistoryMemo.DefaultHistoryMemo.undo();
            onEdit(frame.timeMs);
        },
        redo() {
            HistoryMemo.DefaultHistoryMemo.redo();
            onEdit(frame.timeMs);
        },
        get canUndo() {
            return HistoryMemo.DefaultHistoryMemo.canUndo;
        },
        get canRedo() {
            return HistoryMemo.DefaultHistoryMemo.canRedo;
        },

        /**
         * The frame as the save endpoint wants it. Every mounted label is included, empty
         * or not: a label erased to nothing must be *sent* as empty, or the server would
         * carry the old mask forward and the erase would silently not stick.
         */
        async collect() {
            const planes = {};
            for (const code of stack.codes()) {
                const plane = stack.readPlane(code);
                planes[code] = await encodePlane(plane);
            }
            return {
                timeMs: frame.timeMs,
                width: frame.width,
                height: frame.height,
                planes,
            };
        },

        resize() {
            engine.resize(true, false);
        },

        destroy() {
            element.removeEventListener('wheel', onWheel);
            cornerstone.eventTarget.removeEventListener(
                toolsEnums.Events.SEGMENTATION_DATA_MODIFIED,
                onDataModified
            );
            stack.destroy();
            try {
                ToolGroupManager.destroyToolGroup(ids.toolGroup);
            } catch {
                // Already gone.
            }
            engine.destroy();
            frames.delete(imageId);
            records.delete(imageId);
        },
    };
    return session;
}
