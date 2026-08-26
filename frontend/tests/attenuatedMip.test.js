import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

import {
    DECLARATION_ANCHOR,
    DEFAULT_EXTINCTION,
    SELECTION_ANCHOR,
    SELECTION_ANCHOR_COUNT,
    applyReplacements,
    assertAnchorsPresent,
    attenuatedMipReplacements,
} from '../imaging/grid/attenuatedMip.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..', '..');

/**
 * The real shipped shader. Loaded by file URL because @kitware/vtk.js does not publish
 * this subpath in its `exports` map -- and loading the *real* one is the whole point:
 * a test against a hand-copied excerpt would keep passing after a version bump moved
 * the anchors, which is precisely the failure this module exists to catch.
 */
const { default: VOLUME_FS } = await import(
    pathToFileURL(
        join(REPO, 'node_modules', '@kitware', 'vtk.js', 'Rendering', 'OpenGL', 'glsl', 'vtkVolumeFS.glsl.js')
    )
);

test('the anchors are present in the shipped vtk.js volume shader', () => {
    // This is the version-coupling guard, run against node_modules. If a vtk.js bump
    // moves an anchor, this fails in CI rather than the product silently rendering
    // plain MIP.
    assert.doesNotThrow(() => assertAnchorsPresent(VOLUME_FS));
});

test('the declaration anchor is unique and the selection anchor occurs exactly twice', () => {
    assert.equal(VOLUME_FS.split(DECLARATION_ANCHOR).length - 1, 1);
    assert.equal(VOLUME_FS.split(SELECTION_ANCHOR).length - 1, SELECTION_ANCHOR_COUNT);
});

test('a moved anchor is refused with the anchor named', () => {
    const moved = VOLUME_FS.replace(DECLARATION_ANCHOR, '    // renamed upstream');
    assert.throws(() => assertAnchorsPresent(moved), (error) => {
        assert.match(error.message, /declaration anchor occurs 0 times/);
        assert.match(error.message, /silently fall back to plain MIP/);
        return true;
    });

    // And a selection step that gained or lost an occurrence -- a refactor that
    // unrolled the residual step, say -- is caught too.
    const halved = VOLUME_FS.replace(SELECTION_ANCHOR, '// gone');
    assert.throws(() => assertAnchorsPresent(halved), /selection anchor occurs 1 times/);
});

test('assertAnchorsPresent refuses an empty source rather than passing vacuously', () => {
    assert.throws(() => assertAnchorsPresent(''), /source is required/);
    assert.throws(() => assertAnchorsPresent(undefined), /source is required/);
});

test('the replacement attenuates the comparison and pre-multiplies the colour', () => {
    const spliced = applyReplacements(VOLUME_FS, attenuatedMipReplacements());

    // The two changes that make this amip rather than MIP with extra arithmetic.
    assert.match(spliced, /yggAttenuated = yggDensity \* yggTransmittance/);
    assert.match(spliced, /selectedValue = vec4\(currentValue\.rgb \* yggTransmittance, currentValue\.a\)/);
    assert.match(spliced, /yggTransmittance \*= exp\(-yggDensity \* yggExtinction\)/);

    // The unattenuated selection is gone from both sites.
    assert.ok(!spliced.includes('selectedValue = OP(selectedValue, currentValue);'));
});

test('both selection sites are replaced, so the tail cannot drift from the loop', () => {
    // A version that attenuated the sample loop and not the residual final step would
    // put the last sample on a different scale from every other one.
    const spliced = applyReplacements(VOLUME_FS, attenuatedMipReplacements());
    assert.equal(spliced.split('yggAttenuated > yggBestAttenuated').length - 1, SELECTION_ANCHOR_COUNT);
    assert.equal(spliced.split('yggTransmittance *= exp').length - 1, SELECTION_ANCHOR_COUNT);
});

test('the accumulators are declared once, before the loop that uses them', () => {
    const spliced = applyReplacements(VOLUME_FS, attenuatedMipReplacements());

    const declaration = spliced.indexOf('float yggTransmittance = 1.0;');
    const firstUse = spliced.indexOf('yggDensity * yggTransmittance');
    assert.ok(declaration > 0, 'the accumulators must be declared');
    assert.ok(declaration < firstUse, 'and declared before they are used');
    assert.equal(spliced.split('float yggTransmittance = 1.0;').length - 1, 1);
    assert.equal(spliced.split('float yggBestAttenuated').length - 1, 1);
});

test('the extinction constant is the one carried over from the NiiVue shader', () => {
    // niivue_render_modes.js:82. Keeping the value identical is what makes a
    // side-by-side comparison a check on the port rather than on a re-tuning.
    assert.equal(DEFAULT_EXTINCTION, 0.018);
    assert.match(
        applyReplacements(VOLUME_FS, attenuatedMipReplacements()),
        /const float yggExtinction = 0\.018;/
    );
});

test('the extinction is configurable and emitted as valid GLSL float syntax', () => {
    // GLSL will not coerce an integer literal into a `const float`, so `0` has to be
    // written `0.0` -- a shader that fails to compile takes the whole viewport with it.
    const zero = applyReplacements(VOLUME_FS, attenuatedMipReplacements({ extinction: 0 }));
    assert.match(zero, /const float yggExtinction = 0\.0;/);

    const whole = applyReplacements(VOLUME_FS, attenuatedMipReplacements({ extinction: 1 }));
    assert.match(whole, /const float yggExtinction = 1\.0;/);
});

test('a negative extinction is refused: it would amplify with depth', () => {
    for (const bad of [-0.01, NaN, Infinity, '0.018', null]) {
        assert.throws(
            () => attenuatedMipReplacements({ extinction: bad }),
            /non-negative finite number/,
            `extinction ${JSON.stringify(bad)} should be refused`
        );
    }
});

test('the replacements are shaped the way vtk.js consumes them', () => {
    // `ReplacementShaderMapper.applyShaderReplacements` reads exactly these keys.
    for (const replacement of attenuatedMipReplacements()) {
        assert.equal(replacement.shaderType, 'Fragment');
        assert.equal(typeof replacement.originalValue, 'string');
        assert.equal(typeof replacement.replacementValue, 'string');
        assert.equal(typeof replacement.replaceFirst, 'boolean');
        assert.equal(typeof replacement.replaceAll, 'boolean');
    }
});

test('applying the replacements twice is idempotent', () => {
    // vtk.js rebuilds from a pristine template on every state change, so this should
    // not arise -- but a replacement that reproduced its own anchor would duplicate
    // itself if it ever did, and duplicated declarations are a GLSL compile error that
    // takes the whole viewport with it.
    const once = applyReplacements(VOLUME_FS, attenuatedMipReplacements());
    const twice = applyReplacements(once, attenuatedMipReplacements());
    assert.equal(once, twice);
});
