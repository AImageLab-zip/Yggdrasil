/**
 * The Cornerstone3D runtime configuration Yggdrasil commits to, stated explicitly.
 *
 * Finding F6 of docs/cornerstone-roadmap.md: `ContextPool` and `webGlContextCount: 7`
 * are already the v5 defaults (`@cornerstonejs/core/dist/esm/init.js:11-18`), but we
 * set them anyway. Two reasons:
 *
 *   1. The viewer grid shows 4 viewports (static/js/viewer_grid.js:1291-1315 sizes
 *      four real canvases against the DOM/CSS). 4 <= 7, so the pool never recycles a
 *      context and each viewport keeps its own canvas. Depending on an upstream
 *      default for that property is depending on it silently.
 *   2. `isMobile: true` clamps `webGlContextCount` to 1 (`init.js:59-60`), which would
 *      silently collapse the grid to a single shared context. It must stay false --
 *      touch support (decision #13) is an input concern, not a rendering-budget one.
 */

export const RENDERING_CONFIG = Object.freeze({
    // Never true: see (2) above. Touch is handled by the tool layer.
    isMobile: false,
    rendering: Object.freeze({
        renderingEngineMode: 'contextPool',
        webGlContextCount: 7,
    }),
});

/** Viewports the volume grid lays out; 4 <= webGlContextCount, so no recycling. */
export const GRID_VIEWPORT_COUNT = 4;
