/**
 * The IOS 3D viewport: two STL jaw scans, a trackball, and a surface picker.
 *
 * Cornerstone and vtk.js arrive by injection, exactly as `photos/stackViewport.js` takes
 * them, so everything here is exercisable against a fake in `node --test`. The entry is
 * the only file that imports the real packages.
 *
 * ## Why `ViewportType.VOLUME_3D` and `addActor`, not the "next" geometry path
 *
 * 5.8.2 ships a newer route for meshes: `VOLUME_3D_NEXT` with `renderMode:
 * 'vtkGeometry3d'`. It wants a display-set registry (`getGenericViewportImageDisplaySet`)
 * and a data provider, which is a second lifecycle to learn and keep working for exactly
 * the behaviour `addActor` already gives -- `Mesh` hands back plain `vtkActor`s, and
 * `Viewport.resetCamera` already falls back to `computeVisiblePropBounds` when the default
 * actor is not an image. So: the classic viewport, and the actors added directly.
 *
 * ## Three hazards in the shipped packages, each guarded here
 *
 * - **`TrackballRotateTool.preMouseDownCallback` is unguarded.** It reaches straight into
 *   `viewport.getDefaultActor().actor.getMapper()`. With no actors yet that is a
 *   `TypeError` on mousedown, so the tool group is not made active until both geometries
 *   have resolved and been added.
 * - **`getDefaultActor()` is just the first actor.** Meshes are therefore added *before*
 *   markers and decorations, and a hidden jaw is hidden with `setVisibility(false)` rather
 *   than removed -- otherwise "hide upper" would leave the trackball rotating about a
 *   marker sphere.
 * - **`resetCamera` frames a mesh as a speck.** For `VOLUME_3D` it multiplies the bounds
 *   radius by ten. It is used here only to *read* bounds; the camera is set from
 *   `cameraPresets`.
 *
 * ## Picking
 *
 * Two `vtkCellPicker`s with disjoint pick lists -- one for the jaws, one for the markers --
 * rather than one picker and a hit test afterwards. A marker sits *on* the surface, so a
 * single picker would return whichever the tolerance happened to favour, and selecting an
 * existing landmark would intermittently place a new one on top of it.
 *
 * The canvas-to-display arithmetic lives in `pickMath.js`; see that module for why it is
 * not the identity.
 */

import { cameraFor, distanceForBounds, radiusOf, visibilityFor } from './cameraPresets.js';
import { displayCoordinates, offsetInElement } from './pickMath.js';
import { JAWS } from './landmarkDocument.js';

/**
 * Per-arch colours: a rose upper and a deep blue lower.
 *
 * Red and blue on the maintainer's call. It is also the pairing the Three.js viewer used
 * (`0xffcccc` upper, `0xccccff` lower), so anybody who learned the old viewer reads these
 * arches the same way round.
 *
 * **They differ in lightness as well as hue, and that part is not cosmetic.** An earlier
 * attempt separated two neutrals by hue alone -- 1.17:1 in relative luminance, which is to
 * say not at all once vtk shades them: two surfaces meeting at the occlusal plane are read
 * through lighting that varies far more than that, and the arches looked like one object.
 * This pair is 2.8:1, which survives the shading, a greyscale screenshot, and a red-green
 * colour vision deficiency. The legacy pair had the same weakness -- a light pink against
 * a light lilac is 1.1:1 -- so this is the old scheme with the flaw taken out rather than
 * a return to it.
 *
 * Both stay desaturated. The landmark palette spends red, orange, blue and purple on
 * landmark *types*, and a saturated jaw would compete with the markers sitting on it; the
 * upper is light enough to show surface detail under shading, and the lower is deep
 * without fissures filling in.
 */
export const JAW_COLORS = Object.freeze({
    upper: [240, 176, 172],
    lower: [78, 112, 160],
});

/**
 * The viewport background.
 *
 * Three, not two: the app's own light and dark themes, plus the white a clinician turns on
 * to screenshot a scan for a report.
 */
export const BACKGROUND = Object.freeze({
    dark: [0x15 / 255, 0x20 / 255, 0x36 / 255],
    light: [0xf0 / 255, 0xf0 / 255, 0xf0 / 255],
    white: [1, 1, 1],
});

/** The reference axes, as a fraction of the scans' bounding radius. */
export const AXES_SCALE = 0.1;

const VIEWPORT_ID = 'ios-mesh';
const ENGINE_ID = 'ios-mesh-engine';
const TOOL_GROUP_ID = 'ios-mesh-tools';

/**
 * Build the viewport.
 *
 * @param {object} options
 * @param {HTMLElement} options.element the container to render into.
 * @param {object} options.cornerstone injected: `{RenderingEngine, coreEnums, toolsEnums,
 *   geometryLoader, cache, addTool, ToolGroupManager, tools}`.
 * @param {object} options.vtk injected: `{vtkCellPicker, vtkSphereSource, vtkMapper,
 *   vtkActor}`.
 * @param {(uid: string) => void} [options.onMarkerPicked]
 * @param {(point: number[], jaw: string) => void} [options.onSurfacePicked]
 * @param {(event: PointerEvent) => boolean} [options.shouldPlace]
 * @param {(event: PointerEvent) => boolean} [options.shouldSelect]
 */
export function createMeshViewport({
    element,
    cornerstone,
    vtk,
    onMarkerPicked = () => {},
    onSurfacePicked = () => {},
    shouldPlace = () => false,
    shouldSelect = () => false,
}) {
    const {
        RenderingEngine, coreEnums, toolsEnums, geometryLoader, cache,
        addTool, ToolGroupManager, tools,
    } = cornerstone;
    const { vtkCellPicker, vtkSphereSource, vtkMapper, vtkActor, vtkAxesActor } = vtk;

    const renderingEngine = new RenderingEngine(ENGINE_ID);
    renderingEngine.enableElement({
        viewportId: VIEWPORT_ID,
        type: coreEnums.ViewportType.VOLUME_3D,
        element,
        defaultOptions: { background: BACKGROUND.dark },
    });
    const viewport = renderingEngine.getViewport(VIEWPORT_ID);

    /** `jaw -> {actor, geometryId}`. */
    const meshes = new Map();
    /** `uid -> actor`, rebuilt whenever the marker list changes. */
    const markers = new Map();
    const meshPicker = newPicker(vtkCellPicker);
    const markerPicker = newPicker(vtkCellPicker);
    let toolGroup = null;
    let markerSizeCache = new Map();
    let axesActor = null;

    function newPicker(factory) {
        const picker = factory.newInstance({ opacityThreshold: 0.0001 });
        picker.setPickFromList(true);
        picker.setTolerance(0.001);
        picker.initializePickList();
        return picker;
    }

    function refreshPickList(picker, actors) {
        picker.initializePickList();
        for (const actor of actors) picker.addPickList(actor);
    }

    /**
     * Load one arch and add its actors.
     *
     * The scheme prefix is `mesh:`, which `geometryLoader` self-registers. The loader
     * splits the id on its first colon and fetches the remainder as a URL, so the id is
     * `mesh:` plus the file-serving path. `geometryData` is required -- the loader throws
     * without it rather than defaulting.
     */
    async function loadJaw(jaw, url) {
        const geometryId = `mesh:${url}`;
        const geometry = await geometryLoader.loadAndCacheGeometry(geometryId, {
            type: coreEnums.GeometryType.MESH,
            geometryData: {
                id: geometryId,
                format: coreEnums.MeshType.STL,
                color: JAW_COLORS[jaw],
            },
        });
        const actors = geometry?.data?.actors ?? [];
        if (!actors.length) {
            throw new Error(`The ${jaw} scan loaded no geometry.`);
        }
        // One actor per arch in practice -- an STL is a single polydata -- but the class
        // returns a list, so the first is the one that is tracked and the rest are added
        // so nothing renders half a scan.
        actors.forEach((actor, index) => {
            // **Scalar colouring off, or the arch is red whatever colour we asked for.**
            //
            // `vtkSTLReader` unconditionally sets cell scalars named "Attribute" from the
            // binary STL's per-facet attribute-byte field, and `vtkMapper` ships with
            // `scalarVisibility: true` -- so the mapper colours the surface through its
            // default blue-to-red lookup table and the property colour `Mesh` carefully
            // set is never consulted. That field is a padding word almost every exporter
            // writes as zero, so a whole jaw maps to one end of the rainbow and renders
            // scarlet. It is not data, and this is not a preference: there is nothing
            // meaningful to colour by here.
            actor.getMapper()?.setScalarVisibility?.(false);
            viewport.addActor({ uid: `${jaw}-${index}`, actor });
        });
        meshes.set(jaw, { actor: actors[0], geometryId, visible: true });
    }

    /** Load both arches, then bind the tools. Order matters -- see the header. */
    async function load({ upper, lower }) {
        await Promise.all([loadJaw('upper', upper), loadJaw('lower', lower)]);
        refreshPickList(meshPicker, [...meshes.values()].map((entry) => entry.actor));
        bindTools();
        setCamera('reset');
        viewport.render();
    }

    /**
     * Bind the tool group, once both meshes exist.
     *
     * Deliberately after `load`: `TrackballRotateTool` dereferences the default actor on
     * mousedown with no guard, so a group made active before the geometry resolves is a
     * `TypeError` waiting for the user's first click.
     */
    function bindTools() {
        if (toolGroup) return;
        for (const tool of Object.values(tools)) addTool(tool);
        toolGroup = ToolGroupManager.createToolGroup(TOOL_GROUP_ID);
        for (const [name, tool] of Object.entries(tools)) {
            toolGroup.addTool(tool.toolName ?? name);
        }
        toolGroup.setToolActive(tools.TrackballRotate.toolName, {
            bindings: [{ mouseButton: toolsEnums.MouseBindings.Primary }],
        });
        toolGroup.setToolActive(tools.Pan.toolName, {
            bindings: [{ mouseButton: toolsEnums.MouseBindings.Auxiliary }],
        });
        // Both the right button and the wheel. The wheel is what anybody reaching a 3D
        // scan expects to zoom, and binding only Secondary -- which is what the volume
        // grid's 3D viewport does -- left scrolling doing nothing at all here, on a
        // surface where there is no stack to scroll through instead.
        toolGroup.setToolActive(tools.Zoom.toolName, {
            bindings: [
                { mouseButton: toolsEnums.MouseBindings.Secondary },
                { mouseButton: toolsEnums.MouseBindings.Wheel },
            ],
        });
        toolGroup.addViewport(VIEWPORT_ID, ENGINE_ID);
    }

    /** Display coordinates for a pointer event, re-reading the viewport rect every time. */
    function displayFor(event) {
        const canvas = viewport.canvas;
        const rect = canvas.getBoundingClientRect();
        const { offsetX, offsetY } = offsetInElement(event, rect);
        return displayCoordinates({
            offsetX,
            offsetY,
            canvasWidth: canvas.width,
            canvasHeight: canvas.height,
            // Not cached: the rect moves as other viewports on the page come and go.
            viewport: viewport.getRenderer().getViewport(),
            devicePixelRatio: globalThis.devicePixelRatio || 1,
        });
    }

    function pickWith(picker, event) {
        const renderer = viewport.getRenderer();
        picker.pick(displayFor(event), renderer);
        const picked = picker.getActors?.() ?? [];
        if (!picked.length) return null;
        return { actor: picked[0], position: [...picker.getPickPosition()] };
    }

    /**
     * The pointer handler, in the **capture** phase.
     *
     * Capture is how the legacy tool beat `TrackballControls` to the event and is how this
     * one beats the tool group: a shift-click that placed a landmark must not also rotate
     * the scan out from under it.
     */
    function onPointerDown(event) {
        if (shouldPlace(event)) {
            const hit = pickWith(meshPicker, event);
            if (!hit) return;
            event.preventDefault();
            event.stopImmediatePropagation();
            const jaw = JAWS.find((name) => meshes.get(name)?.actor === hit.actor) ?? null;
            // The picked world position *is* the stored value: the actors carry no
            // transform, so there is no conversion here and there must not be one. See
            // `landmarkMarkers.js`.
            onSurfacePicked(hit.position, jaw);
            return;
        }
        if (shouldSelect(event) && markers.size) {
            const hit = pickWith(markerPicker, event);
            if (!hit) return;
            const uid = [...markers.entries()].find(([, actor]) => actor === hit.actor)?.[0];
            if (!uid) return;
            event.preventDefault();
            event.stopImmediatePropagation();
            onMarkerPicked(uid);
        }
    }
    element.addEventListener('pointerdown', onPointerDown, true);

    /**
     * Replace the marker layer.
     *
     * Rebuilt rather than diffed: a study carries tens of landmarks, the descriptors are
     * cheap, and a diff would need actor identity to survive a redraw -- which is the kind
     * of state that goes stale silently. Sphere sources are cached by radius so a redraw
     * does not re-tessellate a sphere it already has.
     */
    function setMarkers(descriptors) {
        for (const [uid, actor] of markers) {
            viewport.removeActors?.([uid]);
            actor.delete?.();
        }
        markers.clear();

        for (const marker of descriptors) {
            const source = sphereSource(marker.radius);
            const mapper = vtkMapper.newInstance();
            mapper.setInputConnection(source.getOutputPort());
            const actor = vtkActor.newInstance();
            actor.setMapper(mapper);
            // Untransformed, like the meshes: the marker's position is the stored point.
            actor.setPosition(...marker.position);
            const property = actor.getProperty();
            property.setColor(
                ((marker.color >> 16) & 0xff) / 255,
                ((marker.color >> 8) & 0xff) / 255,
                (marker.color & 0xff) / 255,
            );
            property.setOpacity(marker.opacity);
            viewport.addActor({ uid: marker.uid, actor });
            markers.set(marker.uid, actor);
        }
        refreshPickList(markerPicker, [...markers.values()]);
        viewport.render();
    }

    function sphereSource(radius) {
        const key = radius.toFixed(4);
        let source = markerSizeCache.get(key);
        if (!source) {
            source = vtkSphereSource.newInstance({
                radius,
                thetaResolution: 16,
                phiResolution: 12,
            });
            markerSizeCache.set(key, source);
        }
        return source;
    }

    /** Show or hide one arch. Visibility, never removal -- see the header. */
    function setJawVisible(jaw, visible) {
        const entry = meshes.get(jaw);
        if (!entry) return;
        entry.visible = Boolean(visible);
        entry.actor.setVisibility(entry.visible);
        viewport.render();
    }

    function jawVisibility() {
        return Object.fromEntries(
            JAWS.map((jaw) => [jaw, meshes.get(jaw)?.visible !== false]),
        );
    }

    function setWireframe(on) {
        for (const { actor } of meshes.values()) {
            actor.getProperty().setRepresentation(on ? 1 : 2);
        }
        viewport.render();
    }

    /**
     * Set the background.
     *
     * Through the vtk renderer, because **no Cornerstone viewport has a `setBackground`**.
     * `enableElement`'s `defaultOptions.background` is read once, at creation, and the
     * optional call this replaced -- `viewport.setBackground?.(...)` -- therefore did
     * nothing at all, silently, which is exactly what an optional call on a method that
     * does not exist buys you. The White background checkbox moved and nothing happened.
     */
    function setBackground(name) {
        const [red, green, blue] = BACKGROUND[name] ?? BACKGROUND.dark;
        viewport.getRenderer()?.setBackground(red, green, blue);
        viewport.render();
    }

    /**
     * The reference axes.
     *
     * `vtkAxesActor` rather than three hand-built cylinder-and-cone pairs, which is what
     * the legacy viewer assembled. Same three coloured arrows; one upstream class instead
     * of sixty lines, and it is scaled to the scans so the arrows read against a jaw
     * rather than disappearing inside one.
     *
     * Built lazily and never picked: it is not in either picker's list, so it cannot
     * swallow a landmark placement.
     */
    function setAxesVisible(visible) {
        if (!visible && !axesActor) return;
        if (!axesActor) {
            axesActor = vtkAxesActor.newInstance({
                config: { recenter: false, tipLength: 0.2, tipRadius: 0.08, shaftRadius: 0.02 },
            });
            // A tenth of the scan's own radius. The first version scaled by half the
            // *camera distance*, which is already a multiple of the bounding diagonal --
            // so the arrows came out several times longer than the jaws they were meant
            // to orient. This is a small corner marker, not a coordinate frame drawn
            // around the patient.
            const span = (radiusOf(bounds()) || 10) * AXES_SCALE;
            axesActor.setScale(span, span, span);
            viewport.addActor({ uid: 'reference-axes', actor: axesActor });
        }
        axesActor.setVisibility(Boolean(visible));
        viewport.render();
    }

    /** Bounds over the mesh actors, for framing. */
    function bounds() {
        const renderer = viewport.getRenderer();
        return renderer?.computeVisiblePropBounds?.() ?? null;
    }

    /**
     * Point the camera at a named preset.
     *
     * @returns {string[]|null} the arches the preset forces visible, for the caller to
     *   mirror into its own state -- `viewUpper` is a view of one jaw, not just an angle.
     */
    function setCamera(name) {
        const camera = cameraFor(name, distanceForBounds(bounds()));
        if (!camera) return null;
        const shows = visibilityFor(name);
        if (shows) {
            for (const jaw of JAWS) setJawVisible(jaw, shows.includes(jaw));
        }
        viewport.setCamera(camera);
        viewport.render();
        return shows;
    }

    function resize() {
        renderingEngine.resize(true, false);
    }

    function destroy() {
        element.removeEventListener('pointerdown', onPointerDown, true);
        if (toolGroup) ToolGroupManager.destroyToolGroup(TOOL_GROUP_ID);
        for (const { geometryId } of meshes.values()) {
            // Two STLs per navigation is a real leak on a page a clinician tabs through.
            cache.removeGeometryLoadObject?.(geometryId);
        }
        markerSizeCache = new Map();
        renderingEngine.destroy();
    }

    return {
        load,
        setMarkers,
        setAxesVisible,
        setJawVisible,
        jawVisibility,
        setWireframe,
        setBackground,
        setCamera,
        bounds,
        resize,
        destroy,
        get viewport() { return viewport; },
    };
}
