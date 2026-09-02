/**
 * The FDI code drawn inside a tooth outline.
 *
 * What is worth asserting here is the *contract with the drawing helper*, not the pixels:
 * that a label reuses its node across frames rather than piling up a `<text>` per render,
 * that it reports itself touched so the per-frame `clearUntouched` sweep leaves it alone,
 * and that it refuses to draw at a position the caller could not compute -- a centroid of
 * an empty ring is `null`, and `setAttribute('x', 'NaN')` is an invisible label rather than
 * an error anybody would notice.
 *
 * The centring itself is SVG's (`text-anchor` and `dominant-baseline`), which is exactly
 * why it is done that way: there is no arithmetic here to get wrong.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { drawCenteredLabel } from '../imaging/annotations/centeredLabel.js';

/** A stand-in for one `<text>` node, recording what was set on it. */
function fakeNode() {
    return {
        attributes: {},
        textContent: '',
        setAttribute(name, value) { this.attributes[name] = value; },
        getAttribute(name) { return this.attributes[name] ?? null; },
    };
}

/** The `svgDrawingHelper` contract, as `drawingSvg/getSvgDrawingHelper.js` defines it. */
function fakeHelper({ layerId = 'svg-layer-1', hasLayer = true } = {}) {
    const nodes = new Map();
    const touched = [];
    const created = [];
    return {
        nodes,
        touched,
        created,
        svgLayerElement: hasLayer
            ? {
                  id: layerId,
                  ownerDocument: {
                      createElementNS(_ns, tag) {
                          const node = fakeNode();
                          node.tag = tag;
                          created.push(node);
                          return node;
                      },
                  },
              }
            : null,
        getSvgNode: (hash) => nodes.get(hash),
        appendNode: (node, hash) => nodes.set(hash, node),
        setNodeTouched: (hash) => touched.push(hash),
    };
}

describe('drawCenteredLabel', () => {
    it('centres the text on the point it is given', () => {
        const helper = fakeHelper();
        assert.equal(drawCenteredLabel(helper, 'uid-1', 'fdi', '16', [120.5, 80]), true);

        const [node] = helper.created;
        assert.equal(node.tag, 'text');
        assert.equal(node.textContent, '16');
        assert.equal(node.getAttribute('x'), '120.5');
        assert.equal(node.getAttribute('y'), '80');
        // Both axes, and `central` rather than `middle` -- a label of digits has no
        // descenders, so centring on the x-height sits it visibly low in the tooth.
        assert.equal(node.getAttribute('text-anchor'), 'middle');
        assert.equal(node.getAttribute('dominant-baseline'), 'central');
    });

    it('does not swallow clicks aimed at the outline underneath it', () => {
        const helper = fakeHelper();
        drawCenteredLabel(helper, 'uid-1', 'fdi', '16', [0, 0]);
        assert.match(helper.created[0].getAttribute('style'), /pointer-events: none/);
    });

    it('wears the layer drop shadow, and drops it when asked', () => {
        const shadowed = fakeHelper({ layerId: 'layer-a' });
        drawCenteredLabel(shadowed, 'uid-1', 'fdi', '16', [0, 0]);
        assert.match(shadowed.created[0].getAttribute('style'), /url\(#shadow-layer-a\)/);

        const plain = fakeHelper();
        drawCenteredLabel(plain, 'uid-1', 'fdi', '16', [0, 0], { shadow: false });
        assert.doesNotMatch(plain.created[0].getAttribute('style'), /filter/);
    });

    it('reuses its node across frames and reports it touched', () => {
        const helper = fakeHelper();
        drawCenteredLabel(helper, 'uid-1', 'fdi', '16', [10, 10]);
        drawCenteredLabel(helper, 'uid-1', 'fdi', '16', [40, 12]);

        // One node, moved -- not a second one appended. `clearUntouched` only removes what
        // a frame did not touch, so a label that appended every frame would grow the layer
        // without bound and never be swept.
        assert.equal(helper.created.length, 1);
        assert.equal(helper.nodes.size, 1);
        assert.deepEqual(helper.touched, ['uid-1::text::fdi']);
        assert.equal(helper.created[0].getAttribute('x'), '40');
    });

    it('keys the node per annotation, so two teeth do not share one label', () => {
        const helper = fakeHelper();
        drawCenteredLabel(helper, 'uid-1', 'fdi', '16', [10, 10]);
        drawCenteredLabel(helper, 'uid-2', 'fdi', '26', [90, 10]);

        assert.deepEqual([...helper.nodes.keys()], ['uid-1::text::fdi', 'uid-2::text::fdi']);
        assert.deepEqual(helper.created.map((node) => node.textContent), ['16', '26']);
    });

    it('declines to draw what it cannot place', () => {
        for (const position of [null, undefined, [Number.NaN, 0], [0, Number.POSITIVE_INFINITY]]) {
            const helper = fakeHelper();
            assert.equal(drawCenteredLabel(helper, 'uid-1', 'fdi', '16', position), false);
            assert.equal(helper.created.length, 0);
        }
        // And an outline with no tooth picked yet has nothing to say.
        const helper = fakeHelper();
        assert.equal(drawCenteredLabel(helper, 'uid-1', 'fdi', '', [0, 0]), false);
        assert.equal(helper.created.length, 0);
    });

    it('is a no-op before the viewport has an svg layer', () => {
        const helper = fakeHelper({ hasLayer: false });
        assert.equal(drawCenteredLabel(helper, 'uid-1', 'fdi', '16', [0, 0]), false);
    });
});
