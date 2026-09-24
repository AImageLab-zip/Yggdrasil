/**
 * Touch input for the viewers -- the tool-layer half of decision #13.
 *
 * Phones reach the viewers through the public demo (common/mobile.py). Touch is an
 * *input* concern, so it is spelled here and never as a rendering flag:
 * `RENDERING_CONFIG.isMobile` stays false (`config.js`) and the crosshair's `mobile`
 * profile stays off (`grid/layout.js`), because both would also fire on every clinical
 * workstation with a touchscreen attached.
 *
 * The gestures on a phone:
 *
 *   - **one finger scrolls the page**, even when it lands on a viewer. On a phone the
 *     viewers are most of the page; if they kept one finger for themselves there was
 *     almost nowhere left to scroll from, which was how it was reported.
 *   - **two fingers work the view**: pinch to zoom and drag to pan, in one gesture
 *     (`ZoomTool`'s pinch with its `pan` option).
 *
 * Two halves make that true. {@link installPhoneTouchPolicy} decides, per touch, who
 * gets it -- the page for one finger, the viewer for two -- and `theme.css` lets the
 * browser pan the page from a viewer (Cornerstone stamps `touch-action: none` inline on
 * every viewport element). Both apply at phone width only, so a desktop, a tablet and a
 * touchscreen workstation keep exactly the input they had.
 */

/** The same breakpoint the phone CSS uses (`theme.css`, `viewer_grid.css`). */
export const PHONE_QUERY = '(max-width: 640px)';

/** Fingers on the glass that belong to the view rather than the page. */
export const VIEW_TOUCH_POINTS = 2;

/**
 * What a phone touch can land on and still be "the viewer": a Cornerstone viewport
 * (it stamps `data-viewport-uid` on the element it renders into) or a surface that
 * draws itself and opts in with `data-touch-view` (the WSI viewer).
 */
export const VIEW_SELECTOR = '[data-viewport-uid], [data-touch-view]';

/** Touch bindings for the Zoom tool, to append to its mouse bindings. */
export function touchZoomBindings() {
    return [{ numTouchPoints: VIEW_TOUCH_POINTS }];
}

/** Zoom-tool configuration: a two-finger pinch also pans by the fingers' drift. */
export const PINCH_PANS = Object.freeze({ pan: true });

/**
 * Give a tool group its two-finger gesture: pinch to zoom, drag to pan.
 *
 * Additive -- `setToolActive` merges the touch binding with the mouse ones already set,
 * and `pan` is read by `ZoomTool` in its pinch path only, so the mouse is untouched.
 *
 * @param {object} toolGroup a Cornerstone tool group holding `zoomToolName`.
 * @param {string} zoomToolName
 */
export function enableTwoFingerNavigation(toolGroup, zoomToolName) {
    toolGroup.setToolActive(zoomToolName, { bindings: touchZoomBindings() });
    toolGroup.setToolConfiguration(zoomToolName, PINCH_PANS);
}

/** True at phone width. `matchMedia` is injectable for tests. */
export function isPhoneViewport(matchMedia = globalThis.matchMedia) {
    return Boolean(matchMedia?.(PHONE_QUERY)?.matches);
}

/**
 * Route phone touches: one finger to the page, two to the viewer.
 *
 * A document-level capture listener runs before any viewer sees the touch. One finger
 * on a viewer is stopped from propagating -- no tool starts, so nothing competes with
 * the browser's page scroll. Two or more have their default prevented instead, so the
 * page stays still and the viewer's own handlers (Cornerstone's pinch, the WSI pinch)
 * have the gesture to themselves.
 *
 * Idempotent per document: every surface calls it, and the first call wins.
 *
 * @param {Document} [doc]
 * @param {Function} [matchMedia]
 */
export function installPhoneTouchPolicy(doc = globalThis.document, matchMedia = globalThis.matchMedia) {
    if (!doc || doc.__yggTouchPolicy) {
        return;
    }
    doc.__yggTouchPolicy = true;

    const route = (event) => {
        if (!isPhoneViewport(matchMedia)) {
            return;
        }
        if (!event.target?.closest?.(VIEW_SELECTOR)) {
            return;
        }
        if ((event.touches?.length ?? 0) < VIEW_TOUCH_POINTS) {
            event.stopPropagation();
            return;
        }
        if (event.cancelable) {
            event.preventDefault();
        }
    };
    const options = { capture: true, passive: false };
    doc.addEventListener('touchstart', route, options);
    doc.addEventListener('touchmove', route, options);
}

/**
 * Zoom factor between two pairs of pointer positions.
 *
 * @param {{x:number,y:number}[]} previous the two pointers at the last event
 * @param {{x:number,y:number}[]} current the same two pointers now
 * @returns {number} >1 when the fingers moved apart, <1 when they closed; 1 when
 *   either pair is degenerate (both fingers on one pixel), so a bad event never
 *   collapses or explodes the zoom.
 */
export function pinchScale(previous, current) {
    const before = distance(previous);
    const after = distance(current);
    if (!(before > 0) || !(after > 0)) {
        return 1;
    }
    return after / before;
}

/** Midpoint of two pointer positions, the anchor a pinch zooms about. */
export function pinchCentre(points) {
    return { x: (points[0].x + points[1].x) / 2, y: (points[0].y + points[1].y) / 2 };
}

function distance(points) {
    if (!points || points.length < 2) {
        return 0;
    }
    return Math.hypot(points[1].x - points[0].x, points[1].y - points[0].y);
}

/**
 * Two-finger pinch-and-pan on a plain pointer-events surface (the WSI viewer, which
 * draws itself and has no Cornerstone tool group).
 *
 * Tracks every active pointer. While exactly two are down it calls
 * `onPinch(scale, centre, previousCentre)` on each move -- zoom by `scale` about
 * `centre`, and pan by how far the centre drifted -- and reports `true` from
 * `isPinching()`, so the surface's own one-finger handler can stand aside.
 *
 * @param {HTMLElement} element
 * @param {(scale:number, centre:{x:number,y:number}, previousCentre:{x:number,y:number}) => void} onPinch
 *   client coordinates.
 * @returns {{isPinching: () => boolean, destroy: () => void}}
 */
export function trackPinch(element, onPinch) {
    const pointers = new Map();
    let last = null;

    const positions = () => [...pointers.values()];

    function down(e) {
        pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
        last = pointers.size === 2 ? positions() : null;
    }
    function move(e) {
        if (!pointers.has(e.pointerId)) {
            return;
        }
        pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
        if (pointers.size !== 2 || !last) {
            return;
        }
        const now = positions();
        const scale = pinchScale(last, now);
        const previousCentre = pinchCentre(last);
        const centre = pinchCentre(now);
        last = now;
        if (scale !== 1 || centre.x !== previousCentre.x || centre.y !== previousCentre.y) {
            onPinch(scale, centre, previousCentre);
        }
    }
    function up(e) {
        pointers.delete(e.pointerId);
        last = pointers.size === 2 ? positions() : null;
    }

    element.addEventListener('pointerdown', down);
    element.addEventListener('pointermove', move);
    element.addEventListener('pointerup', up);
    element.addEventListener('pointercancel', up);

    return {
        isPinching: () => pointers.size >= 2,
        destroy() {
            element.removeEventListener('pointerdown', down);
            element.removeEventListener('pointermove', move);
            element.removeEventListener('pointerup', up);
            element.removeEventListener('pointercancel', up);
        },
    };
}
