/**
 * The WebGL context budget is a claim about the surfaces, so it is checked against them.
 *
 * `webGlContextCount` is allocated eagerly and *per rendering engine*, and only ever
 * separates multiple STACK viewports inside one engine -- every other viewport type
 * lands on context 0. See `imaging/runtime/config.js` for the full derivation and for
 * the failure it caused when it was seven.
 *
 * These read the surface modules as text rather than importing them: they import
 * Cornerstone, which does not load under `node --test`. Coarse on purpose -- the
 * question is "does any engine gain a second stack viewport", and a grep answers it
 * without pretending to run a renderer.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
    GRID_VIEWPORT_COUNT,
    RENDERING_CONFIG,
    WEBGL_CONTEXTS_PER_ENGINE,
} from '../imaging/runtime/config.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const IMAGING = join(HERE, '..', 'imaging');

const read = (...parts) => readFileSync(join(IMAGING, ...parts), 'utf8');

/** How many `ViewportType.STACK` viewports a module enables. */
function stackViewports(source) {
    return (source.match(/ViewportType\.STACK/g) ?? []).length;
}

/**
 * The surfaces, by the engine each one enables its viewports on.
 *
 * The panoramic is listed under the grid deliberately: `archViewport` and `cprViewport`
 * call `enableElement` on the grid's own engine rather than making a third one, which is
 * the arrangement this budget is derived from.
 */
const ENGINES = {
    'ygg-volume-grid': [
        ['grid', 'viewportManager.js'],
        ['panoramic', 'archViewport.js'],
        ['panoramic', 'cprViewport.js'],
    ],
    'ios-mesh-engine': [['mesh', 'meshViewport.js']],
    // One engine per photo surface: `ygg-photo-<instanceId>`, and the page mounts two.
    'ygg-photo-*': [['photos', 'stackViewport.js']],
};

test('no rendering engine hosts more stack viewports than the pool has contexts', () => {
    for (const [engineId, modules] of Object.entries(ENGINES)) {
        const stacks = modules.reduce((total, parts) => total + stackViewports(read(...parts)), 0);
        assert.ok(
            stacks <= WEBGL_CONTEXTS_PER_ENGINE,
            `${engineId} enables ${stacks} stack viewport(s) but the pool allocates `
                + `${WEBGL_CONTEXTS_PER_ENGINE} context(s) per engine. Raise `
                + 'WEBGL_CONTEXTS_PER_ENGINE to the new maximum, and read that module\'s '
                + 'header first: the count is allocated per engine, so raising it costs '
                + 'contexts on every engine the page builds.'
        );
    }
});

test('the config asks for exactly the budget the surfaces need', () => {
    assert.equal(RENDERING_CONFIG.rendering.webGlContextCount, WEBGL_CONTEXTS_PER_ENGINE);
    assert.equal(RENDERING_CONFIG.rendering.renderingEngineMode, 'contextPool');
});

test('isMobile stays false, so touch support cannot be spelled as a render budget', () => {
    assert.equal(RENDERING_CONFIG.isMobile, false);
});

test('the grid is four viewports and none of them is a stack', () => {
    // The grid's four are what a pool of one has to carry on context 0; if any of them
    // became a STACK the budget derivation above changes.
    assert.equal(GRID_VIEWPORT_COUNT, 4);
    assert.equal(stackViewports(read('grid', 'viewportManager.js')), 0);
});
