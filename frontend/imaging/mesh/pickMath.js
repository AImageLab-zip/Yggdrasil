/**
 * Turning a mouse position into the display coordinates `vtkCellPicker` wants.
 *
 * This is one short function in its own tested module because it is the piece that fails
 * *plausibly*. Get it wrong and picks still land on the mesh -- a few millimetres from
 * where the user clicked. That reads as a coordinate-system bug in the landmark model,
 * and it is not; it is arithmetic about a canvas.
 *
 * ## Why it is not the identity
 *
 * Cornerstone does not give each viewport its own render window. A `RenderingEngine`
 * renders every viewport into one **shared offscreen** canvas and blits the results out,
 * so a viewport occupies a sub-rectangle of that canvas, reported by
 * `renderer.getViewport()` as normalized `[xMin, yMin, xMax, yMax]`. On a maxillo
 * patient-detail page the volume grid and the photo stack mount alongside this surface, so
 * that rectangle is emphatically not `[0, 0, 1, 1]` -- and it changes as viewports come
 * and go, which is why the caller re-reads it on every pick rather than caching it.
 *
 * Two more differences from the obvious version: the canvas backing store is
 * `devicePixelRatio` times the CSS box, and vtk measures y from the **bottom** while the
 * DOM measures it from the top.
 *
 * The arithmetic is transcribed from the one place in the shipped packages that already
 * does it -- `@cornerstonejs/tools/utilities/vtkjs/OrientationControllerWidget/index.js`
 * (`pickAtPosition`) -- rather than re-derived. There was no reason to believe a second
 * derivation would agree, and no cheap way to find out that it did not.
 */

/**
 * Canvas-relative CSS pixels to vtk display coordinates.
 *
 * @param {object} args
 * @param {number} args.offsetX pointer x relative to the canvas' bounding rect, in CSS px.
 * @param {number} args.offsetY pointer y, same.
 * @param {number} args.canvasWidth the canvas *backing store* width (`canvas.width`).
 * @param {number} args.canvasHeight backing store height (`canvas.height`).
 * @param {number[]} args.viewport normalized `[xMin, yMin, xMax, yMax]` from the renderer.
 * @param {number} [args.devicePixelRatio]
 * @returns {number[]} `[x, y, 0]`, y measured from the bottom.
 */
export function displayCoordinates({
    offsetX,
    offsetY,
    canvasWidth,
    canvasHeight,
    viewport,
    devicePixelRatio = 1,
}) {
    const [xMin, yMin, xMax, yMax] = viewport ?? [0, 0, 1, 1];
    const viewportWidth = xMax - xMin;
    const viewportHeight = yMax - yMin;

    const x = offsetX * devicePixelRatio;
    const y = offsetY * devicePixelRatio;

    // `(x / width) * viewportWidth * width` rather than `x * viewportWidth`: the ratio is
    // the fraction across the *canvas*, and the scale back up is by the canvas dimension,
    // so the two `width`s do not cancel -- they are doing different jobs. Written the
    // short way it silently drops the viewport scaling for any non-square canvas.
    const scaledX = (x / canvasWidth) * viewportWidth * canvasWidth;
    const scaledY = (y / canvasHeight) * viewportHeight * canvasHeight;

    return [scaledX, viewportHeight * canvasHeight - scaledY, 0];
}

/**
 * Pointer position relative to an element, in CSS pixels.
 *
 * Split out so the caller can be tested with plain numbers, and because `offsetX` on the
 * event itself is relative to whatever the pointer is *over* -- which, once markers are on
 * screen, is not reliably the canvas.
 */
export function offsetInElement(event, rect) {
    return { offsetX: event.clientX - rect.left, offsetY: event.clientY - rect.top };
}

/**
 * Whether a pointer event should place a landmark.
 *
 * Shift + primary button, which is what the legacy tool required and what the help modal
 * still documents. Kept here rather than inline in the handler so the rule is testable and
 * has one owner: the toolbar, the modal text and the handler all have to agree, and the
 * modal is the one that will not fail a test if it drifts.
 */
export function isPlacementEvent(event) {
    return Boolean(event) && event.button === 0 && Boolean(event.shiftKey);
}

/** A plain select: primary button, no shift, so it does not fight the trackball. */
export function isSelectionEvent(event) {
    return Boolean(event) && event.button === 0 && !event.shiftKey;
}
