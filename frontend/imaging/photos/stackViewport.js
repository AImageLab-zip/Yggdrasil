/**
 * The one module in this surface that touches Cornerstone.
 *
 * Same shape as `grid/viewportManager.js`: Cornerstone is *injected*, not imported, so
 * everything else under `photos/` stays `node --test`-able and only this file needs a
 * GPU. What that buys is stated plainly in the Phase 3 record -- the grid's two shipped
 * defects were both in the region no data-path check looks at, and the way to shrink that
 * region is to keep it small.
 *
 * ## What a photo stack is not
 *
 * Not a volume. There is no third dimension, no affine, no modality and no rescale, so
 * none of `grid/voi.js`, `windowing/autoVoi.js` or `metadata/modalityLutModule.js` is
 * reused: they exist to push a modality preset through a residual NIfTI LUT, and a JPEG
 * has neither a modality nor a LUT. Reusing them would produce an authoritative-looking
 * window on a photograph, which is the mistake decision #16 exists to prevent.
 *
 * ## The window tool is bound only for greyscale
 *
 * Decision #5 says real modality-value windowing only. For an 8-bit photograph there are
 * no modality values at all, and the honest treatments differ by kind:
 *
 * - **RGB** (intraoral, and colour teleradiography scans): the values are sRGB display
 *   triples. vtk's transfer function with `IndependentComponents(false)` applies one
 *   range to all three channels, which is a brightness knob with no physical meaning.
 *   `WindowLevelTool` is not bound and no window is reported. That is the same refusal
 *   `unitFor('cbct') === ''` already makes, one step further.
 * - **Single-component greyscale** (the usual ceph): the values are uncalibrated stored
 *   values, and windowing them is genuinely useful. The tool is bound and the readout
 *   shows `W 256 / L 128` with no unit, because there is not one.
 */

/** One rendering engine for the surface. */
export const PHOTO_RENDERING_ENGINE_ID = 'ygg-photo-stack';

/** The viewport, and its tool group. */
export const PHOTO_VIEWPORT_ID = 'ygg-photo-0';
export const PHOTO_TOOL_GROUP_ID = 'ygg-photo-tools';

/**
 * Tools bound on every stack, colour or not.
 *
 * `StackScroll` on the wheel and `Pan`/`Zoom` on the middle and right buttons, matching
 * the grid so the two surfaces do not need different muscle memory.
 */
export const NAVIGATION_TOOLS = Object.freeze(['Pan', 'Zoom', 'StackScroll']);

/**
 * Measurement tools bound on a photo stack.
 *
 * A subset of the grid's, and the omissions are deliberate: `Probe` reads an intensity,
 * which on a photograph is a display value with no clinical meaning, so offering it would
 * invite a reading nobody should take. Everything geometric is offered, because a length
 * on a calibrated ceph is a real measurement.
 */
export const PHOTO_MEASUREMENT_TOOLS = Object.freeze([
    'Length',
    'Angle',
    'CobbAngle',
    'Bidirectional',
    'RectangleROI',
    'EllipticalROI',
    'CircleROI',
    'Label',
]);

/**
 * Build the stack viewport and return a handle over it.
 *
 * @param {object} options
 * @param {object} options.cornerstone the injected surface -- see the destructuring below.
 * @param {HTMLElement} options.element the viewport host.
 * @returns {object} the handle.
 */
export function createPhotoStack({ cornerstone, element, toolConfiguration = new Map() }) {
    const {
        RenderingEngine,
        coreEnums,
        toolsEnums,
        addTool,
        ToolGroupManager,
        tools,
        annotationState,
        annotationVisibility,
    } = cornerstone;

    const renderingEngine = new RenderingEngine(PHOTO_RENDERING_ENGINE_ID);
    renderingEngine.setViewports([
        {
            viewportId: PHOTO_VIEWPORT_ID,
            type: coreEnums.ViewportType.STACK,
            element,
        },
    ]);
    const viewport = renderingEngine.getViewport(PHOTO_VIEWPORT_ID);

    const toolGroup = createToolGroup({
        addTool,
        ToolGroupManager,
        tools,
        toolsEnums,
        toolConfiguration,
    });
    toolGroup.addViewport(PHOTO_VIEWPORT_ID, PHOTO_RENDERING_ENGINE_ID);

    let imageIds = [];
    let windowingBound = false;

    /**
     * Bind or unbind `WindowLevelTool` for the image now on screen.
     *
     * Re-evaluated per image rather than once per stack: a set can mix a greyscale ceph
     * with colour photographs, and a tool bound for the stack would be active on an image
     * it means nothing for.
     */
    function syncWindowingTool() {
        const image = viewport.csImage ?? viewport.getCornerstoneImage?.();
        const greyscale = (image?.numberOfComponents ?? 1) === 1;
        if (greyscale === windowingBound) {
            return;
        }
        if (!tools.WindowLevel) {
            return;
        }
        if (greyscale) {
            toolGroup.setToolActive(tools.WindowLevel.toolName, {
                bindings: [{ mouseButton: toolsEnums.MouseBindings.Primary }],
            });
        } else {
            toolGroup.setToolDisabled(tools.WindowLevel.toolName);
        }
        windowingBound = greyscale;
    }

    const stack = {
        /** The imageIds currently loaded, in order. */
        imageIds: () => [...imageIds],

        /** The imageId on screen, or null before a stack is set. */
        currentImageId: () => imageIds[viewport.getCurrentImageIdIndex?.() ?? 0] ?? null,

        currentIndex: () => viewport.getCurrentImageIdIndex?.() ?? 0,

        async setStack(nextImageIds, startIndex = 0) {
            imageIds = [...nextImageIds];
            await viewport.setStack(imageIds, startIndex);
            syncWindowingTool();
            viewport.render();
        },

        async scrollTo(index) {
            if (index < 0 || index >= imageIds.length) {
                return;
            }
            await viewport.setImageIdIndex(index);
            syncWindowingTool();
            viewport.render();
        },

        /**
         * Make one measurement tool primary, passiving the others.
         *
         * Passive rather than disabled for the rest, so an existing annotation stays
         * draggable: `addTool` writes no `toolOptions` entry, and a mode-less tool is
         * skipped by `getToolsWithModesForElement` -- which is exactly the defect the
         * grid shipped with, where restored measurements were not drawn until a tool
         * button happened to be clicked.
         */
        setPrimaryTool(name) {
            for (const toolName of PHOTO_MEASUREMENT_TOOLS) {
                if (!tools[toolName]) continue;
                const real = tools[toolName].toolName;
                if (toolName === name) {
                    toolGroup.setToolActive(real, {
                        bindings: [{ mouseButton: toolsEnums.MouseBindings.Primary }],
                    });
                } else {
                    toolGroup.setToolPassive(real);
                }
            }
            if (name === null) {
                syncWindowingTool();
            }
        },

        /**
         * Turn measuring on or off wholesale.
         *
         * On: every measurement tool gets a mode, so restored annotations render. Off:
         * they are disabled and the primary button goes back to windowing or panning.
         */
        setAnnotationMode(enabled) {
            for (const toolName of PHOTO_MEASUREMENT_TOOLS) {
                if (!tools[toolName]) continue;
                const real = tools[toolName].toolName;
                if (enabled) {
                    toolGroup.setToolPassive(real);
                } else {
                    toolGroup.setToolDisabled(real);
                }
            }
            if (!enabled) {
                windowingBound = false;
                syncWindowingTool();
            }
            viewport.render();
        },

        /** Everything Cornerstone is holding, for the caller to filter and group. */
        readAnnotations() {
            const groups = annotationState?.getAllAnnotations?.() ?? [];
            return Array.isArray(groups) ? groups : [];
        },

        /**
         * Put stored annotations back, keyed by the image each belongs to.
         *
         * The stored `isVisible` is **not** restored, and that is the grid's fix carried
         * over rather than rediscovered: `setAnnotationVisibility(uid, true)` only clears
         * the flag for a UID already in Cornerstone's hidden set, which a freshly added
         * annotation never is -- so an annotation saved while hidden would come back
         * invisible *and* unreachable. Visibility is session state.
         *
         * @param {Map<string, object[]>} byImageId
         */
        restoreAnnotations(byImageId) {
            const add = annotationState?.addAnnotation;
            if (typeof add !== 'function') {
                return 0;
            }
            let restored = 0;
            for (const [imageId, entries] of byImageId) {
                for (const entry of entries) {
                    const annotation = {
                        ...entry,
                        isVisible: undefined,
                        metadata: { ...(entry.metadata ?? {}), referencedImageId: imageId },
                    };
                    delete annotation.isVisible;
                    add(annotation, element);
                    restored += 1;
                }
            }
            viewport.render();
            return restored;
        },

        /** Remove every measurement from the viewer. The server is not told. */
        clearAnnotations(toolNames) {
            const remove = annotationState?.removeAnnotation;
            const all = annotationState?.getAllAnnotations?.() ?? [];
            if (typeof remove !== 'function') {
                return 0;
            }
            let removed = 0;
            for (const entry of all) {
                if (!toolNames.includes(entry?.metadata?.toolName)) continue;
                if (!entry.annotationUID) continue;
                remove(entry.annotationUID);
                removed += 1;
            }
            viewport.render();
            return removed;
        },

        /** Show or hide the measurements, leaving any navigation state alone. */
        setAnnotationsVisible(visible, toolNames) {
            const setVisibility = annotationVisibility?.setAnnotationVisibility;
            const all = annotationState?.getAllAnnotations?.() ?? [];
            for (const entry of all) {
                if (!toolNames.includes(entry?.metadata?.toolName)) continue;
                if (!entry.annotationUID) continue;
                setVisibility?.(entry.annotationUID, visible);
                // Written as well as toggled, so the call is idempotent -- the hidden set
                // and the flag can otherwise disagree after a restore.
                entry.isVisible = visible;
            }
            viewport.render();
        },

        resize() {
            renderingEngine.resize(true, false);
        },

        destroy() {
            try {
                ToolGroupManager.destroyToolGroup(PHOTO_TOOL_GROUP_ID);
            } catch {
                // Already gone; nothing to undo.
            }
            renderingEngine.destroy();
        },
    };

    return stack;
}

function createToolGroup({ addTool, ToolGroupManager, tools, toolsEnums, toolConfiguration }) {
    for (const name of [...NAVIGATION_TOOLS, ...PHOTO_MEASUREMENT_TOOLS, 'WindowLevel']) {
        if (tools[name]) {
            addTool(tools[name]);
        }
    }

    // Destroy first: a re-mount on the same page (switching tabs, or the RGB editor
    // writing a new row) would otherwise find the group already registered and throw.
    try {
        ToolGroupManager.destroyToolGroup(PHOTO_TOOL_GROUP_ID);
    } catch {
        // No previous group.
    }
    const toolGroup = ToolGroupManager.createToolGroup(PHOTO_TOOL_GROUP_ID);

    for (const name of [...NAVIGATION_TOOLS, ...PHOTO_MEASUREMENT_TOOLS, 'WindowLevel']) {
        if (tools[name]) {
            // Per-tool configuration goes in here, not on the tool class: `addTool` on the
            // *group* is what builds the instance the viewport uses, so a config set
            // anywhere else is read by nothing.
            toolGroup.addTool(tools[name].toolName, toolConfiguration.get(tools[name].toolName) ?? {});
        }
    }

    if (tools.StackScroll) {
        toolGroup.setToolActive(tools.StackScroll.toolName, {
            bindings: [{ mouseButton: toolsEnums.MouseBindings.Wheel }],
        });
    }
    if (tools.Pan) {
        toolGroup.setToolActive(tools.Pan.toolName, {
            bindings: [{ mouseButton: toolsEnums.MouseBindings.Auxiliary }],
        });
    }
    if (tools.Zoom) {
        toolGroup.setToolActive(tools.Zoom.toolName, {
            bindings: [{ mouseButton: toolsEnums.MouseBindings.Secondary }],
        });
    }
    // Measurement tools start Disabled, matching the grid: annotation is a mode, off by
    // default, so a study being read shows fewer controls than one being measured.
    for (const name of PHOTO_MEASUREMENT_TOOLS) {
        if (tools[name]) {
            toolGroup.setToolDisabled(tools[name].toolName);
        }
    }
    return toolGroup;
}
