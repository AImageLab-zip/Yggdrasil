'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const renderModes = require('../modality_viewers/niivue_render_modes.js');

test('volumeRange maps robust limits into full-range Level and Window', () => {
    const result = renderModes.volumeRange({
        global_min: -1000,
        global_max: 3000,
        robust_min: -600,
        robust_max: 2200
    });

    assert.deepEqual(result, {
        min: -1000,
        max: 3000,
        robustMin: -600,
        robustMax: 2200,
        level: 45,
        window: 70
    });
});

test('volumeRange safely falls back for invalid or flat metadata', () => {
    const result = renderModes.volumeRange({
        global_min: NaN,
        global_max: NaN,
        cal_min: 7,
        cal_max: 7
    });

    assert.equal(result.min, 7);
    assert.equal(result.max, 8);
    assert.equal(result.level, 50);
    assert.equal(result.window, 100);
});

test('windowCuts uses center and width semantics and clamps controls', () => {
    assert.deepEqual(renderModes.windowCuts(50, 40), [0.3, 0.7]);
    assert.deepEqual(renderModes.windowCuts(-20, 200), [-0.5, 0.5]);
    assert.deepEqual(renderModes.windowCuts(100, 1), [0.995, 1.005]);
});

test('render shader splice replaces only the ray loop and preserves the overlay tail', () => {
    const source = `#version 300 es
out vec4 fColor;
void main() {
bool overlayIsClipCutaway = isClipAllVolumes && isClipCutaway;
ORIGINAL_RAY_LOOP
if (firstHit.a < len) {
    gl_FragDepth = frac2ndc(firstHit.xyz);
}
OVERLAY_PASS
}`;
    const result = renderModes.spliceRenderFragment(source, 'CUSTOM_RAY_LOOP');

    assert.ok(result.includes('uniform vec2 yggWindow;'));
    assert.ok(result.includes('CUSTOM_RAY_LOOP'));
    assert.ok(!result.includes('ORIGINAL_RAY_LOOP'));
    assert.ok(result.includes('OVERLAY_PASS'));
    assert.equal(renderModes.spliceRenderFragment(source.replace('out vec4 fColor;', ''), 'LOOP'), null);
});

test('slice shader splice injects live windowing after the base sample', () => {
    const source = `out vec4 color;
void main() {
    vec4 background = texture(volume, texPos);
    color = background;
}`;
    const result = renderModes.spliceSliceFragment(source);

    assert.ok(result.includes('uniform vec2 yggWindow;'));
    assert.ok(result.indexOf('float yggWidth') > result.indexOf('texture(volume, texPos)'));
    assert.equal(renderModes.spliceSliceFragment('void main() {}'), null);
});

function createMockNiiVue(overrides = {}) {
    class MockShader {
        constructor(gl, vertex, fragment) {
            this.program = { vertex, fragment };
            this.uniforms = {
                volume: 1,
                colormap: 2,
                overlay: 3,
                drawing: 4,
                paqd: 5,
                drawOpacity: 6,
                yggWindow: 7
            };
        }

        use() {}
    }

    const vertexShader = { type: 'vertex', source: 'slice vertex' };
    const fragmentShader = {
        type: 'fragment',
        source: `out vec4 color;
void main() {
    vec4 background = texture(volume, texPos);
    color = background;
}`
    };
    const gl = {
        SHADER_TYPE: 'shader-type',
        VERTEX_SHADER: 'vertex',
        getAttachedShaders: () => [vertexShader, fragmentShader],
        getShaderSource: shader => shader.source,
        getShaderParameter: shader => shader.type,
        uniform1i: () => {},
        uniform1f: () => {},
        uniform2f: () => {}
    };
    const volume = {
        global_min: -1000,
        global_max: 3000,
        robust_min: -500,
        robust_max: 2000,
        cal_min: -500,
        cal_max: 2000
    };
    const nv = {
        gl,
        volumes: [volume],
        sliceMMShader: new MockShader(gl, vertexShader.source, fragmentShader.source),
        customSliceShader: null,
        drawOpacity: 1,
        drawScene: () => {},
        updateGLVolume: () => {},
        opts: {},
        ...overrides
    };
    return { nv, volume };
}

test('controller uploads the global range before assigning the live slice shader', () => {
    const calls = [];
    const { nv, volume } = createMockNiiVue();
    nv.updateGLVolume = () => {
        calls.push({
            calMin: volume.cal_min,
            calMax: volume.cal_max,
            customInstalled: nv.customSliceShader !== null
        });
    };

    const controller = renderModes.createController(nv);

    assert.deepEqual(calls, [{ calMin: -1000, calMax: 3000, customInstalled: false }]);
    assert.ok(nv.customSliceShader);
    assert.equal(controller.isLiveWindowing(), true);
});

test('native shaded fallback exposes handled async completion', async () => {
    const { nv } = createMockNiiVue({
        setVolumeRenderIllumination: async () => {
            await Promise.resolve();
        }
    });
    const controller = renderModes.createController(nv);

    const result = controller.setMode('shaded');
    assert.equal(result.pending, true);
    assert.ok(result.ready instanceof Promise);

    const completed = await result.ready;
    assert.equal(completed.available, true);
    assert.equal(completed.pending, false);
    assert.equal(completed.fallback, true);
});

test('native shaded fallback converts async rejection into an unavailable result', async () => {
    const { nv } = createMockNiiVue({
        setVolumeRenderIllumination: async () => {
            throw new Error('GPU failure');
        }
    });
    const controller = renderModes.createController(nv);

    const completed = await controller.setMode('shaded').ready;
    assert.equal(completed.available, false);
    assert.equal(completed.pending, false);
    assert.match(completed.message, /failed/i);
});
