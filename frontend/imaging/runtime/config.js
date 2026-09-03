/**
 * The Cornerstone3D runtime configuration Yggdrasil commits to, stated explicitly.
 *
 * ## `webGlContextCount` is per *rendering engine*, and the pool is eager
 *
 * This is the setting behind "Too many active WebGL contexts. Oldest context will be
 * lost." on a patient carrying several modalities, and behind the black intraoral
 * viewport, the missing IOS mesh, the `vtkPolyDataVS` compile failure and the
 * `Cannot read properties of null (reading 'isUniformUsed')` crash that follow it --
 * those are all one lost context being drawn into.
 *
 * `ContextPoolRenderingEngine.ensureContextPool` builds `new WebGLContextPool(count)`
 * **per engine**, and that constructor allocates every context up front
 * (`WebGLContextPool.js:5-17`) whether a viewport ever asks for one. A maxillo patient
 * page runs four engines -- `ygg-volume-grid` (the grid, with the panoramic's two
 * viewports deliberately sharing it), `ios-mesh-engine`, `ygg-photo-stack` and
 * `ygg-photo-intraoral` -- so at 7 it allocated **28** offscreen contexts on a page that
 * uses four. Browsers keep on the order of 16 alive and silently drop the oldest, which
 * is exactly the message and exactly the two surfaces that went dark.
 *
 * ## Why {@link WEBGL_CONTEXTS_PER_ENGINE} is 1, and what would change it
 *
 * The pool is not a per-viewport allocation. `addVtkjsDrivenViewport` assigns
 * `contextIndex = 0` for every viewport type **except** `STACK`, which alone spreads
 * with `this._viewports.size % contexts.length`
 * (`ContextPoolRenderingEngine.js:61-71`). So the count only ever buys separation
 * between multiple *stack* viewports inside one engine.
 *
 * Yggdrasil has none of those. Per engine:
 *
 *   - `ygg-volume-grid`: 3 ORTHOGRAPHIC + 1 VOLUME_3D ({@link GRID_VIEWPORT_COUNT}),
 *     plus the panoramic's ORTHOGRAPHIC arch and VOLUME_3D strip -- no STACK, so all of
 *     them shared context 0 at 7 exactly as they do at 1.
 *   - `ios-mesh-engine`: one VOLUME_3D.
 *   - `ygg-photo-stack` and `ygg-photo-intraoral`: one STACK each, in engines of their
 *     own -- which is what makes a pool of one correct rather than merely cheaper.
 *
 * The number to raise this to is therefore "the most STACK viewports any single engine
 * hosts", and `frontend/tests/renderingBudget.test.js` pins that at one against the
 * surfaces as built. Give an engine a second stack viewport and that test fails, which
 * is the point: the budget is a claim about the code, not a preference.
 *
 * `isMobile` must stay false. It clamps `webGlContextCount` to 1 (`init.js:59-60`) --
 * harmless now that we ask for 1 anyway, but touch support (decision #13) is an input
 * concern and must not be spelled as a rendering-budget one.
 */

/**
 * Offscreen WebGL contexts each rendering engine allocates up front.
 *
 * One per engine, not one per viewport -- see this module's header for why that is the
 * whole budget and what would make it wrong.
 */
export const WEBGL_CONTEXTS_PER_ENGINE = 1;

export const RENDERING_CONFIG = Object.freeze({
    // Never true: see the header. Touch is handled by the tool layer.
    isMobile: false,
    rendering: Object.freeze({
        renderingEngineMode: 'contextPool',
        webGlContextCount: WEBGL_CONTEXTS_PER_ENGINE,
    }),
});

/** Viewports the volume grid lays out, all of them non-STACK: see the header. */
export const GRID_VIEWPORT_COUNT = 4;
