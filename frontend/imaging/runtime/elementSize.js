/**
 * Knowing when a viewport's container can actually be measured.
 *
 * Every surface in this codebase mounts into an element that may be `display: none` at
 * the time -- a hidden tab, a `d-none` placeholder swapped in once the data arrives --
 * and Cornerstone builds a 0x0 canvas when it is. `resize()` is the documented way to
 * pick that container up afterwards, so what each surface needs is a signal for *when*.
 *
 * This lived in `grid/bootstrap.js`, was copied privately into `photos/bootstrap.js`,
 * and was missing entirely from the video surface -- which is why the laparoscopy
 * annotator rendered a black box: its viewport was created while the element still
 * carried `d-none`, the class was removed a moment later, and nothing ever told
 * Cornerstone the canvas now had a size. One copy, in the runtime layer that the three
 * surfaces already share.
 */

/**
 * Whether an element can currently be measured.
 *
 * Used to decide when to *resize*, never whether to mount: a surface that waits for
 * visibility before mounting waits forever on a page whose tab is never opened, which
 * is the failure `bootstrapVolumeGrid` records at length.
 *
 * @param {HTMLElement} element
 * @returns {boolean}
 */
export function isMeasurable(element) {
    return Boolean(element) && element.offsetParent !== null && element.clientWidth > 0;
}

/**
 * Call back whenever an element's size changes, and once it first has one.
 *
 * Falls back to the window's `resize` event where `ResizeObserver` is missing, which is
 * what the photos surface did on its own: it does not catch a tab being shown, but it is
 * strictly better than never resizing at all.
 *
 * @param {HTMLElement} element
 * @param {(measurable: boolean) => void} callback
 * @returns {() => void} disconnect.
 */
export function observeSize(element, callback) {
    const view = element?.ownerDocument?.defaultView ?? globalThis;
    if (!element) {
        return () => {};
    }
    if (typeof view.ResizeObserver !== 'function') {
        const onResize = () => callback(isMeasurable(element));
        view.addEventListener?.('resize', onResize);
        return () => view.removeEventListener?.('resize', onResize);
    }
    const observer = new view.ResizeObserver(() => callback(isMeasurable(element)));
    observer.observe(element);
    return () => observer.disconnect();
}
