/**
 * Dragging a modality chip onto a grid window.
 *
 * `templates/brain/patient_detail_content.html` still renders `draggable="true"` chips
 * and a `.drop-hint` in every window, and `static/css/viewer_grid.css` still styles
 * `.drag-over` — the markup and the styling survived 3.0 intact. What `c03afa6` deleted
 * with `viewer_grid.js` was the ~120 lines that bound them, so the chips have been
 * decorative ever since and every window showed the same series.
 *
 * The handlers are the pre-3.0 ones (`viewer_grid.js:1078-1194`), which were correct;
 * only what a drop *does* has changed, from constructing a NiiVue instance to calling
 * the grid's own `loadVolumeIntoWindows`. Two details in them are load-bearing and are
 * kept for the reasons they were there:
 *
 *   - **`resolveWindowDropTarget` walks up with `closest`.** The events fire on the
 *     canvas and the overlay inside the window, not on the window, so binding to the
 *     window and reading `e.target` gets a child element.
 *   - **`dragleave` ignores a `relatedTarget` still inside the window.** Without it the
 *     highlight flickers off every time the pointer crosses a child boundary.
 *
 * **Touch has its own path.** HTML5 drag-and-drop does not start from a finger on most
 * phone browsers, so on the public demo (common/mobile.py) the chips were inert again.
 * A touch or pen pointer on a chip is tracked with pointer events instead: drag it
 * onto a window and let go, or tap it (it is *armed*) and then tap a window. The mouse
 * keeps the native path above, untouched.
 *
 * Pure DOM: the grid is passed in. `node --test` can drive all of it.
 */

/** The chips, as the brain template renders them. */
export const CHIP_SELECTOR = '.modality-chip[data-modality]';

/** Class the CSS already defines for a window under the pointer. */
export const DRAG_OVER_CLASS = 'drag-over';

/** A chip tapped on a touch screen, waiting for the window it goes to. */
export const ARMED_CLASS = 'is-armed';

/** How far a finger travels on a chip before a tap becomes a drag, in CSS px. */
export const TOUCH_DRAG_THRESHOLD_PX = 8;

/** The MIME type the payload travels under, plus a plain-text fallback. */
const JSON_TYPE = 'application/json';
const TEXT_TYPE = 'text/plain';

/** The `.viewer-window` an event landed in, or null. */
export function resolveWindowDropTarget(target) {
    if (!target || typeof target.closest !== 'function') {
        return null;
    }
    return target.closest('.viewer-window[data-window-index]');
}

/**
 * Bind chips and windows so a chip dropped on a window loads that modality there.
 *
 * @param {object} options
 * @param {Document} options.doc
 * @param {HTMLElement[]} options.elements the window elements, in window order.
 * @param {(windowIndex: number, slug: string) => Promise<unknown>} options.onDrop
 * @returns {() => void} unbind.
 */
export function bindDragDrop({ doc, elements, onDrop }) {
    const chips = Array.from(doc.querySelectorAll(CHIP_SELECTOR));
    const bound = [];

    const on = (node, type, handler, capture = false) => {
        node.addEventListener(type, handler, capture);
        bound.push(() => node.removeEventListener(type, handler, capture));
    };

    for (const chip of chips) {
        on(chip, 'dragstart', (event) => {
            const slug = chip.dataset.modality;
            event.dataTransfer.setData(TEXT_TYPE, slug);
            event.dataTransfer.setData(JSON_TYPE, JSON.stringify({ modality: slug }));
            event.dataTransfer.effectAllowed = 'copy';
            chip.classList.add('is-dragging');
        });
        on(chip, 'dragend', () => chip.classList.remove('is-dragging'));
    }

    bindTouchDragDrop({ doc, chips, elements, onDrop, on });

    for (const element of elements) {
        if (!element) {
            continue;
        }
        // Capture, as before: the canvas and the viewport overlay sit on top of the
        // window and would otherwise swallow the events.
        on(element, 'dragover', (event) => {
            const windowEl = resolveWindowDropTarget(event.target) || element;
            // Without preventDefault the browser refuses the drop, silently.
            event.preventDefault();
            event.dataTransfer.dropEffect = 'copy';
            windowEl.classList.add(DRAG_OVER_CLASS);
        }, true);

        on(element, 'dragleave', (event) => {
            const windowEl = resolveWindowDropTarget(event.target) || element;
            // Moving between children of the same window is not leaving it.
            if (event.relatedTarget && windowEl.contains(event.relatedTarget)) {
                return;
            }
            windowEl.classList.remove(DRAG_OVER_CLASS);
        }, true);

        on(element, 'drop', (event) => {
            const windowEl = resolveWindowDropTarget(event.target) || element;
            event.preventDefault();
            windowEl.classList.remove(DRAG_OVER_CLASS);

            const slug = readDroppedModality(event.dataTransfer);
            const windowIndex = Number(windowEl.dataset.windowIndex);
            if (!slug || !Number.isInteger(windowIndex)) {
                return;
            }
            onDrop(windowIndex, slug);
        }, true);
    }

    return () => bound.forEach((unbind) => unbind());
}

/**
 * The finger's half of `bindDragDrop`: pointer events, for touch and pen only.
 *
 * `setPointerCapture` keeps every move and the release on the chip, however far the
 * finger travels, so the window under it is found with `elementFromPoint` rather than
 * from the event target -- which, under capture, is always the chip.
 */
function bindTouchDragDrop({ doc, chips, elements, onDrop, on }) {
    const windows = elements.filter(Boolean);
    let armed = null;
    let gesture = null;
    let hovered = null;

    const isTouch = (event) => event.pointerType === 'touch' || event.pointerType === 'pen';

    const windowAt = (x, y) => {
        const hit = resolveWindowDropTarget(doc.elementFromPoint?.(x, y) ?? null);
        return hit && windows.includes(hit) ? hit : null;
    };
    const hover = (windowEl) => {
        if (hovered === windowEl) {
            return;
        }
        hovered?.classList.remove(DRAG_OVER_CLASS);
        hovered = windowEl;
        hovered?.classList.add(DRAG_OVER_CLASS);
    };
    const disarm = () => {
        armed?.classList.remove(ARMED_CLASS);
        armed = null;
    };
    const drop = (windowEl, slug) => {
        const windowIndex = Number(windowEl.dataset.windowIndex);
        if (slug && Number.isInteger(windowIndex)) {
            onDrop(windowIndex, slug);
        }
    };

    for (const chip of chips) {
        on(chip, 'pointerdown', (event) => {
            if (!isTouch(event)) {
                return;
            }
            gesture = { chip, pointerId: event.pointerId, x: event.clientX, y: event.clientY, dragging: false };
            chip.setPointerCapture?.(event.pointerId);
        });
        on(chip, 'pointermove', (event) => {
            if (!gesture || gesture.chip !== chip || event.pointerId !== gesture.pointerId) {
                return;
            }
            const moved = Math.hypot(event.clientX - gesture.x, event.clientY - gesture.y);
            if (!gesture.dragging && moved >= TOUCH_DRAG_THRESHOLD_PX) {
                gesture.dragging = true;
                disarm();
                chip.classList.add('is-dragging');
            }
            if (gesture.dragging) {
                event.preventDefault?.();
                hover(windowAt(event.clientX, event.clientY));
            }
        });
        const finish = (event, cancelled) => {
            if (!gesture || gesture.chip !== chip || event.pointerId !== gesture.pointerId) {
                return;
            }
            const { dragging } = gesture;
            gesture = null;
            chip.classList.remove('is-dragging');
            hover(null);
            if (cancelled) {
                return;
            }
            if (dragging) {
                const target = windowAt(event.clientX, event.clientY);
                if (target) {
                    drop(target, chip.dataset.modality);
                }
                return;
            }
            // A tap: arm this chip, or disarm it if it already was.
            const wasArmed = armed === chip;
            disarm();
            if (!wasArmed) {
                armed = chip;
                chip.classList.add(ARMED_CLASS);
            }
        };
        on(chip, 'pointerup', (event) => finish(event, false));
        on(chip, 'pointercancel', (event) => finish(event, true));
    }

    // The second tap of tap-to-place. Capture, for the same reason as the drag
    // handlers above: the canvas and overlay sit on top of the window.
    for (const element of windows) {
        on(element, 'pointerup', (event) => {
            if (!armed || !isTouch(event)) {
                return;
            }
            const windowEl = resolveWindowDropTarget(event.target) || element;
            const slug = armed.dataset.modality;
            disarm();
            drop(windowEl, slug);
        }, true);
    }
}

/**
 * The modality slug out of a drop, whichever way it was carried.
 *
 * The JSON entry is the one the chips write; the plain-text fallback is what a chip
 * dragged from another window or an older page would carry, and reading it costs one
 * line rather than a broken drop.
 *
 * @param {DataTransfer} dataTransfer
 * @returns {string|null}
 */
export function readDroppedModality(dataTransfer) {
    try {
        const parsed = JSON.parse(dataTransfer.getData(JSON_TYPE));
        if (parsed?.modality) {
            return String(parsed.modality);
        }
    } catch {
        // Fall through to the text form.
    }
    const text = dataTransfer.getData(TEXT_TYPE);
    return text ? String(text) : null;
}
