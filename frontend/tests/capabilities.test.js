import test from 'node:test';
import assert from 'node:assert/strict';

import { detectCapabilities, UNSUPPORTED_MESSAGE } from '../imaging/runtime/capabilities.js';

/** A document whose canvas returns `contexts[type]` from getContext. */
function fakeDocument(contexts, { throwOnGetContext = false } = {}) {
    return {
        createElement(tag) {
            assert.equal(tag, 'canvas');
            return {
                getContext(type) {
                    if (throwOnGetContext) {
                        throw new Error('context creation blocked');
                    }
                    return contexts[type] ?? null;
                },
            };
        },
    };
}

test('WebGL2 present and WebGPU absent is supported', () => {
    const caps = detectCapabilities({
        document: fakeDocument({ webgl2: {} }),
        navigator: {},
    });
    assert.deepEqual(caps, { webgl2: true, webgpu: false, supported: true, message: null });
});

test('WebGPU is reported but never required (decision #13)', () => {
    const caps = detectCapabilities({
        document: fakeDocument({ webgl2: {} }),
        navigator: { gpu: {} },
    });
    assert.equal(caps.webgpu, true);
    assert.equal(caps.supported, true);
});

test('WebGPU alone is not enough -- WebGL2 is required, not preferred', () => {
    const caps = detectCapabilities({
        document: fakeDocument({}),
        navigator: { gpu: {} },
    });
    assert.equal(caps.webgpu, true);
    assert.equal(caps.webgl2, false);
    assert.equal(caps.supported, false);
    assert.equal(caps.message, UNSUPPORTED_MESSAGE);
});

test('a getContext that throws is unsupported, not a crash', () => {
    // Hardened and headless environments throw here rather than returning null.
    const caps = detectCapabilities({
        document: fakeDocument({ webgl2: {} }, { throwOnGetContext: true }),
        navigator: {},
    });
    assert.equal(caps.supported, false);
});

test('no document at all (worker, SSR, node) is unsupported, not a crash', () => {
    const caps = detectCapabilities({ document: undefined, navigator: undefined });
    assert.equal(caps.supported, false);
    assert.equal(caps.message, UNSUPPORTED_MESSAGE);
});

test('the unsupported message names WebGL2 and tells the user what to do', () => {
    // It is shown to clinicians, so it must be actionable rather than a stack trace.
    assert.match(UNSUPPORTED_MESSAGE, /WebGL2/);
    assert.match(UNSUPPORTED_MESSAGE, /hardware acceleration/);
});
