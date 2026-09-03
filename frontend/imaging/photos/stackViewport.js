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

/**
 * Cornerstone ids for one instance of this surface.
 *
 * Per instance, because a patient-detail page mounts two: teleradiography and the intraoral
 * photographs. `createToolGroup` destroys and recreates its group by id on mount, so two
 * stacks sharing one id would mean the second mount silently stole the first's tools.
 *
 * @param {string} instanceId
 */
export function stackIds(instanceId) {
    return Object.freeze({
        renderingEngine: `ygg-photo-${instanceId}`,
        viewport: `ygg-photo-${instanceId}-0`,
        toolGroup: `ygg-photo-${instanceId}-tools`,
    });
}

/** The historical ids, kept as the default instance's. */
export const PHOTO_RENDERING_ENGINE_ID = stackIds('stack').renderingEngine;
export const PHOTO_VIEWPORT_ID = stackIds('stack').viewport;
export const PHOTO_TOOL_GROUP_ID = stackIds('stack').toolGroup;

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
 * The tooth-outline tool, bound on the same viewport rather than a second one.
 *
 * Intraoral photographs are a photo stack that also carries segmentation, not a different
 * kind of surface: they need the same stack scroll, the same pan and zoom, and the same
 * calibration and measurement tools. A second rendering engine for the contours would hold
 * a second GPU context on the same page and would need its own copy of all of that.
 *
 * It is listed separately from the measurement tools because it is a *different mode*.
 * `setAnnotationMode` and `setSegmentationMode` are two switches, and only one of them is
 * on at a time -- a click that could either measure or draw a tooth would do the wrong one
 * about half the time.
 */
export const SEGMENTATION_TOOLS = Object.freeze(['ToothOutline']);

/**
 * Build the stack viewport and return a handle over it.
 *
 * @param {object} options
 * @param {object} options.cornerstone the injected surface -- see the destructuring below.
 * @param {HTMLElement} options.element the viewport host.
 * @returns {object} the handle.
 */
export function createPhotoStack({
    cornerstone,
    element,
    toolConfiguration = new Map(),
    instanceId = 'stack',
}) {
    const {
        RenderingEngine,
        coreEnums,
        toolsEnums,
        addTool,
        ToolGroupManager,
        tools,
        annotationState,
        annotationVisibility,
        stackPrefetch,
    } = cornerstone;
    const ids = stackIds(instanceId);

    const renderingEngine = new RenderingEngine(ids.renderingEngine);
    renderingEngine.setViewports([
        {
            viewportId: ids.viewport,
            type: coreEnums.ViewportType.STACK,
            element,
        },
    ]);
    const viewport = renderingEngine.getViewport(ids.viewport);

    const toolGroup = createToolGroup({
        addTool,
        ToolGroupManager,
        tools,
        toolsEnums,
        toolConfiguration,
        toolGroupId: ids.toolGroup,
    });
    toolGroup.addViewport(ids.viewport, ids.renderingEngine);

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

        /**
         * Load the stack, and start fetching the images the user has not asked for yet.
         *
         * `viewport.setStack` decodes exactly one image -- the one on screen -- so on the
         * intraoral surface, where a study is five photographs a clinician steps through
         * one after another, every Next was a fresh network round trip and a decode with
         * nothing on screen in the meantime.
         *
         * `stackPrefetch` is upstream's own answer and is what the stack surfaces in
         * OHIF use: it queues the rest through `imageLoadPoolManager` at
         * `RequestType.Prefetch`, which is *below* interaction priority, so a prefetch
         * cannot delay the image the user is actually looking at. Its default
         * `maxImagesToPrefetch` is `Infinity`; a photo study is five images and a
         * teleradiograph is one, so the whole stack is cached and no cap is configured
         * for a limit neither surface can reach.
         *
         * Re-armed on every `setStack` rather than once at mount, because calibration
         * rebuilds the stack to make the metadata provider re-read -- and `enable` is
         * idempotent, it re-registers its own listeners.
         */
        async setStack(nextImageIds, startIndex = 0) {
            imageIds = [...nextImageIds];
            await viewport.setStack(imageIds, startIndex);
            syncWindowingTool();
            viewport.render();
            if (imageIds.length > 1) {
                stackPrefetch?.enable?.(element);
            }
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
         * Run `handler` whenever the image on screen changes, however it changed.
         *
         * The Prev/Next buttons are not the only way to move: `StackScroll` is bound to the
         * wheel, so a mouse wheel changes the image without going through `scrollTo` at all.
         * Everything that has to follow the current image -- the counter, the calibration
         * readout, which image the tooth editor is drawing -- was hanging off the buttons
         * only, so a wheel scroll left all three describing the image before last.
         *
         * `STACK_NEW_IMAGE` is Cornerstone's own signal and fires for both paths, which is
         * why the buttons now go through here as well rather than calling the handler twice.
         *
         * @param {() => void} handler
         * @returns {() => void} unsubscribe.
         */
        onImageChange(handler) {
            const listener = () => {
                syncWindowingTool();
                handler();
            };
            element.addEventListener(coreEnums.Events.STACK_NEW_IMAGE, listener);
            return () => element.removeEventListener(coreEnums.Events.STACK_NEW_IMAGE, listener);
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

        /**
         * Turn tooth outlining on or off.
         *
         * On: the contour tool takes the primary button and every measurement tool is
         * disabled, so a click draws a tooth and nothing else. Off: it is set *passive*
         * rather than disabled, which is what keeps restored outlines drawn while the tab
         * is merely being read -- a mode-less tool is skipped by
         * `getToolsWithModesForElement`, which is exactly the defect the grid shipped with.
         *
         * @param {boolean} enabled
         */
        setSegmentationMode(enabled) {
            const tool = tools.ToothOutline;
            if (!tool) {
                return;
            }
            if (enabled) {
                for (const name of PHOTO_MEASUREMENT_TOOLS) {
                    if (tools[name]) {
                        toolGroup.setToolDisabled(tools[name].toolName);
                    }
                }
                if (tools.WindowLevel) {
                    toolGroup.setToolDisabled(tools.WindowLevel.toolName);
                    windowingBound = false;
                }
                toolGroup.setToolActive(tool.toolName, {
                    bindings: [{ mouseButton: toolsEnums.MouseBindings.Primary }],
                });
            } else {
                toolGroup.setToolPassive(tool.toolName);
                syncWindowingTool();
            }
            viewport.render();
        },

        /**
         * Put one stored tooth outline back on the viewer.
         *
         * Hand-built rather than routed through `SplineROITool.hydrate`, for one concrete
         * reason: `hydrate` hardcodes `data.label = ''`, and the label *is* the FDI code --
         * the only field the tool's `getTextLines(data)` can read. Everything else it does
         * is reproduced here, including leaving `data.spline.instance` unset: the tool
         * builds it on first render (`SplineROITool.js:610-612`), so constructing a spline
         * here would be a second, unused one.
         *
         * @param {object} outline `{imageId, label, worldPoints, splineType, toolName}`
         * @returns {object|null} the annotation added.
         */
        addToothOutline({ imageId, label, worldPoints, splineType, toolName }) {
            const add = annotationState?.addAnnotation;
            if (typeof add !== 'function' || !worldPoints?.length) {
                return null;
            }
            const camera = viewport.getCamera();
            const annotation = {
                annotationUID: cornerstone.uuid?.() ?? undefined,
                data: {
                    // The FDI code. See `toothOutlines.js` for why it lives here.
                    label,
                    handles: { points: worldPoints.map((point) => [...point]) },
                    contour: { closed: true },
                    spline: { type: splineType },
                    cachedStats: {},
                },
                highlighted: false,
                autoGenerated: false,
                invalidated: true,
                isLocked: false,
                isVisible: true,
                metadata: {
                    toolName,
                    referencedImageId: imageId,
                    FrameOfReferenceUID: viewport.getFrameOfReferenceUID?.(),
                    viewPlaneNormal: [...(camera.viewPlaneNormal ?? [0, 0, 1])],
                    viewUp: [...(camera.viewUp ?? [0, -1, 0])],
                },
            };
            add(annotation, element);
            return annotation;
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

        /**
         * The pixel dimensions of one loaded image, or null.
         *
         * Read off the loaded Cornerstone image rather than the metadata registry: the
         * registry holds what the server said, and a vertex has to be clamped to the
         * bytes actually on screen. The two disagree exactly when they matter -- after the
         * RGB editor has cropped a photograph.
         *
         * @param {string} [imageId] defaults to the image on screen.
         * @returns {{width: number, height: number}|null}
         */
        imageBounds(imageId) {
            const image = viewport.csImage ?? viewport.getCornerstoneImage?.();
            if (!image || (imageId && stack.currentImageId() !== imageId)) {
                return null;
            }
            return image.width && image.height
                ? { width: image.width, height: image.height }
                : null;
        },

        /**
         * Frame a rectangle given in image pixels.
         *
         * `parallelScale` is half the viewport's world height, so the rectangle is fitted
         * against whichever of its dimensions is tighter against the element's aspect
         * ratio -- fitting only the height would push a wide shape off both sides.
         *
         * @param {object} region `{imageId, minX, maxX, minY, maxY}`
         * @param {number} [margin] extra world units around the region.
         */
        frameImageRegion({ imageId, minX, maxX, minY, maxY }, margin = 1.15) {
            const convert = cornerstone.imageToWorld;
            if (typeof convert !== 'function') {
                return;
            }
            const target = imageId ?? stack.currentImageId();
            if (!target) {
                return;
            }
            const topLeft = convert([minX, minY], target);
            const bottomRight = convert([maxX, maxY], target);
            const focalPoint = topLeft.map((value, index) => (value + bottomRight[index]) / 2);
            const worldHeight = Math.abs(bottomRight[1] - topLeft[1]);
            const worldWidth = Math.abs(bottomRight[0] - topLeft[0]);
            const { clientWidth, clientHeight } = element;
            const aspect = clientWidth && clientHeight ? clientWidth / clientHeight : 1;
            const scale = Math.max(worldHeight, worldWidth / aspect) / 2;
            const camera = viewport.getCamera();
            viewport.setCamera({
                ...camera,
                focalPoint,
                position: camera.position.map(
                    (value, index) => focalPoint[index] + (value - camera.focalPoint[index])
                ),
                parallelScale: Math.max(scale * margin, 1e-3),
            });
            viewport.render();
        },

        /** Back to the whole image, undoing a zoom or a frame. */
        resetCamera() {
            viewport.resetCamera();
            viewport.render();
        },

        resize() {
            renderingEngine.resize(true, false);
        },

        destroy() {
            // Before the tool group: `disable` clears the queued prefetch requests, and a
            // surface being torn down must not leave a pool fetching images for a viewport
            // that no longer exists.
            stackPrefetch?.disable?.(element);
            try {
                ToolGroupManager.destroyToolGroup(ids.toolGroup);
            } catch {
                // Already gone; nothing to undo.
            }
            renderingEngine.destroy();
        },
    };

    return stack;
}

function createToolGroup({ addTool, ToolGroupManager, tools, toolsEnums, toolConfiguration, toolGroupId }) {
    for (const name of [...NAVIGATION_TOOLS, ...PHOTO_MEASUREMENT_TOOLS, ...SEGMENTATION_TOOLS, 'WindowLevel']) {
        if (tools[name]) {
            addTool(tools[name]);
        }
    }

    // Destroy first: a re-mount on the same page (switching tabs, or the RGB editor
    // writing a new row) would otherwise find the group already registered and throw.
    try {
        ToolGroupManager.destroyToolGroup(toolGroupId);
    } catch {
        // No previous group.
    }
    const toolGroup = ToolGroupManager.createToolGroup(toolGroupId);

    for (const name of [...NAVIGATION_TOOLS, ...PHOTO_MEASUREMENT_TOOLS, ...SEGMENTATION_TOOLS, 'WindowLevel']) {
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
    // Measurement and segmentation tools start Disabled, matching the grid: both are
    // modes, off by default, so a study being read shows fewer controls than one being
    // measured or segmented.
    for (const name of [...PHOTO_MEASUREMENT_TOOLS, ...SEGMENTATION_TOOLS]) {
        if (tools[name]) {
            toolGroup.setToolDisabled(tools[name].toolName);
        }
    }
    return toolGroup;
}
