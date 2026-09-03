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
 * Pure DOM: the grid is passed in. `node --test` can drive all of it.
 */

/** The chips, as the brain template renders them. */
export const CHIP_SELECTOR = '.modality-chip[data-modality]';

/** Class the CSS already defines for a window under the pointer. */
export const DRAG_OVER_CLASS = 'drag-over';

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
