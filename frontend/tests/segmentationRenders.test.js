import test from 'node:test';
import assert from 'node:assert/strict';

import { coalesceSegmentationRenders } from '../imaging/runtime/segmentationRenders.js';

// Cornerstone's real scheduler, not a stand-in: it schedules on `window`.
globalThis.window ??= globalThis;
const { segmentationRenderingEngine: engine } = await import(
    '@cornerstonejs/tools/segmentation/SegmentationRenderingEngine'
);

test('a burst of segmentation renders reaches every viewport', () => {
    const frames = [];
    globalThis.requestAnimationFrame = (callback) => frames.push(callback);
    const rendered = [];
    engine._triggerRender = (viewportId) => {
        rendered.push(viewportId);
        if (viewportId === 'a') {
            engine.renderSegmentationsForViewport('during'); // as mounting an actor does
        }
    };
    coalesceSegmentationRenders(engine);

    engine.renderSegmentationsForViewport('a');
    // What `addSegmentations` asks for: a segmentation no viewport shows yet, so no ids.
    // Unpatched, this request stops the queue and strands everything behind it.
    engine.renderSegmentation('not-shown-anywhere');
    engine.renderSegmentationsForViewport('b');
    while (frames.length) {
        frames.shift()();
    }

    assert.deepEqual(rendered.sort(), ['a', 'b', 'during']);
});
