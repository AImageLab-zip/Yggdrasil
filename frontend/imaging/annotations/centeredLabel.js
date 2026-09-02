/**
 * A short label drawn *inside* an annotation instead of beside it.
 *
 * Cornerstone's own answer is `renderLinkedTextBoxAnnotation`: a text box parked off the
 * annotation's right edge, nudged around by an overlap registry, with a dashed leader line
 * back to the nearest vertex. That is the right shape for a measurement -- `Area 42 mm²`
 * is several words long, it must not cover the thing being measured, and the reader needs
 * the line to know which shape it belongs to.
 *
 * An FDI code is two digits naming the shape it sits in. There is nothing to disambiguate,
 * the leader line is longer than the label, and thirty-two of them fanned out to the right
 * of a photograph is a column of numbers rather than a labelled arch. So: no box, no
 * leader, no registry -- the digits, centred.
 *
 * ## Why this is not `drawTextBox` with a computed offset
 *
 * `drawTextBox` positions a `<text x="0" y="0">` with `dy="1.2em"` tspans inside a
 * translated group, so centring through it means predicting the rendered glyph box.
 * Upstream does exactly that in `getTextBoxCoordsCanvas` -- `estimatedCharWidth = 8` -- and
 * it can afford to, because it only needs a rough rectangle to avoid overlaps. Centring is
 * not rough: eight pixels of error on a two-character label is half a character. SVG
 * already centres text exactly, in the renderer, with the real font metrics, so the answer
 * is to ask it.
 *
 * ## The `svgDrawingHelper` contract
 *
 * The same one every `drawingSvg/*` helper uses, and the same node-hash shape
 * (`uid::type::nodeUID`), so a label participates in the per-frame `clearUntouched` sweep:
 * an annotation that stops rendering -- deleted, hidden, scrolled off -- takes its label
 * with it. Nothing here imports Cornerstone, which is what keeps it testable against a
 * fake helper.
 */

const SVG_NS = 'http://www.w3.org/2000/svg';

/**
 * Draw (or move) one centred, unboxed text label.
 *
 * @param {object} svgDrawingHelper from the tool's render context.
 * @param {string} annotationUID
 * @param {string} labelUID unique within the annotation.
 * @param {string} text
 * @param {number[]|null} position `[x, y]` in canvas coordinates -- the label's centre.
 * @param {object} [style] `{color, fontFamily, fontSize, shadow}`.
 * @returns {boolean} whether anything was drawn.
 */
export function drawCenteredLabel(
    svgDrawingHelper,
    annotationUID,
    labelUID,
    text,
    position,
    style = {},
) {
    const layer = svgDrawingHelper?.svgLayerElement;
    if (!layer || !text || !Number.isFinite(position?.[0]) || !Number.isFinite(position?.[1])) {
        return false;
    }
    const {
        color = 'rgb(255, 255, 255)',
        fontFamily = 'Helvetica, Arial, sans-serif',
        fontSize = '14px',
        shadow = true,
    } = style;

    const attributes = {
        x: String(position[0]),
        y: String(position[1]),
        fill: color,
        'font-family': fontFamily,
        'font-size': fontSize,
        'text-anchor': 'middle',
        // `central`, not `middle`: `middle` centres on the x-height, which for a label of
        // digits -- no descenders, no lowercase -- sits visibly low in the shape.
        'dominant-baseline': 'central',
        // Not `visible`, unlike upstream's text boxes: those are draggable handles and
        // have to be hit-testable. This one is painted on the tooth, and a label that
        // swallowed clicks would make the middle of every outline unselectable.
        style:
            'user-select: none; pointer-events: none;' +
            // The drop shadow `store/addEnabledElement.js` defines per svg layer. Two
            // digits over a photograph need it: the palette runs from a deep blue to a
            // pale yellow, so no single ink is legible against every tooth without one.
            (shadow ? ` filter: url(#shadow-${layer.id});` : ''),
    };

    const hash = `${annotationUID}::text::${labelUID}`;
    const existing = svgDrawingHelper.getSvgNode(hash);
    const node = existing ?? layer.ownerDocument.createElementNS(SVG_NS, 'text');
    for (const [name, value] of Object.entries(attributes)) {
        if (node.getAttribute?.(name) !== value) {
            node.setAttribute(name, value);
        }
    }
    if (node.textContent !== text) {
        node.textContent = text;
    }
    if (existing) {
        svgDrawingHelper.setNodeTouched(hash);
    } else {
        node.setAttribute('data-annotation-uid', annotationUID);
        svgDrawingHelper.appendNode(node, hash);
    }
    return true;
}
