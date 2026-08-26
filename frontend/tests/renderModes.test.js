import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

import {
    BLEND_MODES,
    DEFAULT_RENDER_MODE,
    RENDER_MODES,
    RENDER_MODE_LABELS,
    applyRenderMode,
    assertBlendModesMatch,
    renderModeSpec,
} from '../imaging/grid/renderModes.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..', '..');

test('the inlined blend-mode numbers are vtk.js own', async () => {
    const { BlendMode } = await import(
        pathToFileURL(
            join(REPO, 'node_modules', '@kitware', 'vtk.js', 'Rendering', 'Core', 'VolumeMapper', 'Constants.js')
        )
    );
    assert.doesNotThrow(() => assertBlendModesMatch(BlendMode));
    assert.equal(BLEND_MODES.MAXIMUM_INTENSITY_BLEND, BlendMode.MAXIMUM_INTENSITY_BLEND);
    assert.equal(BLEND_MODES.COMPOSITE_BLEND, BlendMode.COMPOSITE_BLEND);
});

test('a drifted blend-mode number is refused, since it would just pick another projection', () => {
    assert.throws(
        () => assertBlendModesMatch({ COMPOSITE_BLEND: 0, MAXIMUM_INTENSITY_BLEND: 9 }),
        /MAXIMUM_INTENSITY_BLEND: expected 1, got 9/
    );
});

test('the modes and labels match the template the toolbar renders', () => {
    // templates/maxillo/patient_detail_content.html -- the <option> values and text.
    assert.deepEqual(RENDER_MODES, ['mip', 'amip', 'shaded']);
    assert.deepEqual(RENDER_MODE_LABELS, {
        mip: 'MIP',
        amip: 'Attenuated MIP',
        shaded: 'Shaded Volume',
    });
    assert.equal(DEFAULT_RENDER_MODE, 'amip', 'the template marks amip selected');
});

test('amip is a maximum-intensity projection WITH the attenuation replacements', () => {
    // The attenuation changes what the maximum is taken over, not the projection. And
    // the replacements only substitute inside the MIP branch, so any other blend mode
    // would leave them compiled out and silently render plain MIP.
    const spec = renderModeSpec('amip');
    assert.equal(spec.blendMode, BLEND_MODES.MAXIMUM_INTENSITY_BLEND);
    assert.ok(spec.shaderReplacements.length > 0);
    assert.ok(spec.shaderReplacements.every((r) => r.shaderType === 'Fragment'));
});

test('plain mip is the same projection with NO replacements', () => {
    const spec = renderModeSpec('mip');
    assert.equal(spec.blendMode, BLEND_MODES.MAXIMUM_INTENSITY_BLEND);
    assert.deepEqual(spec.shaderReplacements, []);
});

test('mip and amip differ only in the shader, which is the whole point', () => {
    const mip = renderModeSpec('mip');
    const amip = renderModeSpec('amip');
    assert.equal(mip.blendMode, amip.blendMode);
    assert.equal(mip.shade, amip.shade);
    assert.notDeepEqual(mip.shaderReplacements, amip.shaderReplacements);
});

test('lighting is off for both projections and on for shaded', () => {
    // Lighting is meaningless under a maximum-intensity projection: the output is one
    // sampled value per ray, not an accumulated surface.
    for (const mode of ['mip', 'amip']) {
        const spec = renderModeSpec(mode);
        assert.equal(spec.shade, false, mode);
        assert.equal(spec.diffuse, 0, mode);
    }
    const shaded = renderModeSpec('shaded');
    assert.equal(shaded.blendMode, BLEND_MODES.COMPOSITE_BLEND);
    assert.equal(shaded.shade, true);
    assert.ok(shaded.diffuse > 0);
});

test('an unknown mode is refused rather than defaulted', () => {
    assert.throws(() => renderModeSpec('volume'), /Unknown render mode/);
    assert.throws(() => renderModeSpec(undefined), /Unknown render mode/);
});

test('applying a mode sets the replacements BEFORE the blend mode', () => {
    // `getNeedToRebuildShaders` compares both, so setting the blend mode last means the
    // rebuild it triggers already sees the replacements. The reverse works only by
    // relying on the rebuild firing twice.
    const calls = [];
    const actor = {
        getMapper: () => ({
            getViewSpecificProperties: () => ({ existing: true }),
            setViewSpecificProperties: (value) => calls.push(['replacements', value]),
            setBlendMode: (value) => calls.push(['blendMode', value]),
        }),
        getProperty: () => ({
            setShade: (v) => calls.push(['shade', v]),
            setAmbient: () => {},
            setDiffuse: () => {},
            setSpecular: () => {},
        }),
    };

    applyRenderMode(actor, 'amip');
    const order = calls.map(([name]) => name);
    assert.ok(order.indexOf('replacements') < order.indexOf('blendMode'), order.join(','));

    // And existing view-specific properties are preserved, not clobbered.
    const [, payload] = calls.find(([name]) => name === 'replacements');
    assert.equal(payload.existing, true);
    assert.ok(Array.isArray(payload.OpenGL.ShaderReplacements));
});

test('applying a mode returns the spec that was applied', () => {
    const actor = {
        getMapper: () => ({
            getViewSpecificProperties: () => null,
            setViewSpecificProperties: () => {},
            setBlendMode: () => {},
        }),
        getProperty: () => ({
            setShade: () => {},
            setAmbient: () => {},
            setDiffuse: () => {},
            setSpecular: () => {},
        }),
    };
    assert.equal(applyRenderMode(actor, 'shaded').label, 'Shaded Volume');
});
