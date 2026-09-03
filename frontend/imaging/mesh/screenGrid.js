/**
 * The screen-space grid overlay.
 *
 * A reticle, not a measurement: N evenly spaced lines across the viewport in *screen*
 * coordinates, used to compare two scans by eye. It does not move with the camera and has
 * no relationship to the mesh, so it has no Cornerstone or vtk equivalent and does not
 * want one -- a world-space `GridHelper` would be a different tool that happened to look
 * similar.
 *
 * Ported from `ios.js:492-598` unchanged in behaviour: a 2D canvas layered over the
 * viewport, `pointer-events: none`, hidden by default. Split into "where the lines are"
 * (pure, tested) and "draw them" (a canvas call) so the arithmetic is not locked inside a
 * rendering context.
 */

/** Grid sizes the dropdown offers. */
export const GRID_SIZES = Object.freeze([3, 9, 15, 20]);

/** The legacy default: a 9x9 grid, off until asked for. */
export const DEFAULT_GRID_SIZE = 9;

export const GRID_STROKE = 'rgba(0, 0, 0, 0.4)';
export const GRID_LINE_WIDTH = 2;

/**
 * The line segments for a grid of `divisions` cells over a `width` x `height` box.
 *
 * `divisions + 1` lines each way, because the two edges are drawn: the legacy grid framed
 * the viewport as well as dividing it, and a grid missing its border reads as misaligned.
 *
 * @returns {number[][]} `[[x1, y1, x2, y2], ...]`, verticals then horizontals.
 */
export function gridLines(divisions, width, height) {
    const count = Math.max(1, Math.floor(divisions) || 1);
    if (!(width > 0) || !(height > 0)) return [];
    const lines = [];
    const cellWidth = width / count;
    const cellHeight = height / count;
    for (let index = 0; index <= count; index += 1) {
        const x = index * cellWidth;
        lines.push([x, 0, x, height]);
    }
    for (let index = 0; index <= count; index += 1) {
        const y = index * cellHeight;
        lines.push([0, y, width, y]);
    }
    return lines;
}

/** Paint a grid onto a 2D context, or clear it when `divisions` is falsy. */
export function drawGrid(canvas, divisions) {
    const context = canvas?.getContext?.('2d');
    if (!context) return;
    const { width, height } = canvas;
    context.clearRect(0, 0, width, height);
    if (!divisions) return;
    context.strokeStyle = GRID_STROKE;
    context.lineWidth = GRID_LINE_WIDTH;
    for (const [x1, y1, x2, y2] of gridLines(divisions, width, height)) {
        context.beginPath();
        context.moveTo(x1, y1);
        context.lineTo(x2, y2);
        context.stroke();
    }
}

/**
 * The overlay canvas, sized to its host and kept out of the way of the pointer.
 *
 * `pointer-events: none` is not cosmetic: without it the overlay would swallow every
 * pick, and the failure would look like a dead viewport rather than a stray canvas.
 */
export function createOverlay(documentRef, host) {
    const canvas = documentRef.createElement('canvas');
    canvas.id = 'grid-overlay';
    Object.assign(canvas.style, {
        position: 'absolute',
        top: '0',
        left: '0',
        width: '100%',
        height: '100%',
        pointerEvents: 'none',
        zIndex: '10',
        display: 'none',
    });
    host.appendChild(canvas);
    return canvas;
}

/** Match the backing store to the host's box. Returns true when it changed. */
export function resizeOverlay(canvas, host) {
    const width = host?.clientWidth ?? 0;
    const height = host?.clientHeight ?? 0;
    if (!(width > 0) || !(height > 0)) return false;
    if (canvas.width === width && canvas.height === height) return false;
    canvas.width = width;
    canvas.height = height;
    return true;
}
