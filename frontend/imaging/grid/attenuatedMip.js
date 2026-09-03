/**
 * `amip` — depth-attenuated MIP — ported onto vtk.js's volume shader.
 *
 * Finding F7 says `amip` "has no Cornerstone equivalent" and needs sign-off before
 * deletion, which reads as though the mode has to go. It does not. `amip` is not a
 * blend mode vtk.js is missing; it is maximum-intensity projection whose running
 * maximum is taken over `density * transmittance` instead of over `density`, with the
 * transmittance decaying front to back. That is a change to five lines inside an
 * existing loop, and F18 records the supported hook for making it:
 * `vtkOpenGLVolumeMapper` mixes in
 * `ReplacementShaderMapper.implementBuildShadersWithReplacements`, so a mapper accepts
 * `getViewSpecificProperties().OpenGL.ShaderReplacements` — substitutions against **any
 * literal string**, not only a `//VTK::` anchor.
 *
 * It matters because `amip` is `selected` at
 * `templates/maxillo/patient_detail_content.html:88`: it is what clinicians actually
 * look at. Replacing it with plain MIP would flatten every volume render in the
 * product and would not throw.
 *
 * **This is the same technique the code it replaces already uses.**
 * `niivue_render_modes.js` splices its own GLSL into NiiVue's shader anchored on three
 * literal strings, and `static/js/tests/niivue_render_modes.test.js` pins them. The
 * fragility is identical and so is the mitigation: {@link assertAnchorsPresent} is
 * called against the real shader source at start-up, and the test runs it against the
 * copy in `node_modules`, so a vtk.js bump that moves an anchor fails loudly instead of
 * silently rendering plain MIP.
 *
 * Scope, stated rather than assumed:
 *
 *   - **Single-component volumes only.** `getTextureValue` returns a `vec4`; the scalar
 *     is `.r` for a grayscale volume, which is what a CBCT and every MRI series here
 *     is. A multi-component volume would need the component-mixing path and is not
 *     handled — {@link attenuatedMipReplacements} is simply not applied there.
 *   - **The mapper must be in `MAXIMUM_INTENSITY_BLEND`.** The replacements live inside
 *     that `#if` branch; attaching them to a composite mapper substitutes nothing and
 *     {@link assertAnchorsPresent} would not catch it, because the anchors are still
 *     present in the source — they are just compiled out.
 *   - **The extinction constant is by-eye**, carried over verbatim from
 *     `niivue_render_modes.js:82`. It has to be looked at on real studies whichever way
 *     the mode is implemented, which is F7's original requirement and is unchanged.
 */

/**
 * Beer-Lambert extinction per unit density, carried over from
 * `static/js/modality_viewers/niivue_render_modes.js:82`.
 *
 * "Enough front-to-back shadowing to retain depth without hiding dense anatomy behind
 * the first surface" is the whole specification, and it was arrived at by eye. Kept as
 * a named constant with the same value so a side-by-side comparison is comparing the
 * port and not a re-tuning.
 */
export const DEFAULT_EXTINCTION = 0.018;

/**
 * Where the per-ray accumulators are declared: the top of the MIP branch.
 *
 * Unique in the shader source, verified by {@link assertAnchorsPresent}.
 */
export const DECLARATION_ANCHOR = '    // Find maximum/minimum intensity along the ray.';

/**
 * The selection step, which appears **twice** — once in the sample loop and once for
 * the residual final step. Both are replaced (`replaceAll`), so the two cannot drift:
 * a version that attenuated the loop and not the tail would put the last sample on a
 * different scale from every other one.
 */
export const SELECTION_ANCHOR = [
    '      vec4 previousSelectedValue = selectedValue;',
    '      vec4 currentValue = getTextureValue(posIS);',
    '      selectedValue = OP(selectedValue, currentValue);',
    '      if (previousSelectedValue != selectedValue) {',
    '        selectedPosVC = posVC;',
    '        selectedPosIS = posIS;',
    '      }',
].join('\n');

/** How many times the selection anchor must occur. A change here is a shader change. */
export const SELECTION_ANCHOR_COUNT = 2;

/**
 * The accumulator declarations.
 *
 * Deliberately does **not** re-emit {@link DECLARATION_ANCHOR}. vtk.js rebuilds shaders
 * from a pristine template on every state change (`getReplacedShaderTemplate` resets
 * `shaders.Fragment` before substituting), so double application should not arise --
 * but a replacement that reproduces its own anchor is one that duplicates itself if it
 * ever does, and the resulting GLSL would fail to compile and take the viewport with
 * it. Not reproducing the anchor makes a second pass a no-op instead.
 */
function declarations(extinction) {
    return [
        '    // Yggdrasil: depth-attenuated MIP, replacing the legacy `amip` mode.',
        '    // Maximum intensity along the ray, weighted by front-to-back extinction.',
        '    float yggTransmittance = 1.0;',
        '    // Starts below any density so the first sample always wins; the branch',
        '    // below never falls through to an uninitialised selection.',
        '    float yggBestAttenuated = -1.0;',
        `    const float yggExtinction = ${formatFloat(extinction)};`,
    ].join('\n');
}

/**
 * The attenuated selection step.
 *
 * Differs from the original in exactly two ways: the comparison is against
 * `density * transmittance` rather than the raw value, and the selected colour is
 * pre-multiplied by the transmittance so a deep maximum renders dimmer than a shallow
 * one of the same density. That second part is what produces the depth cue; without it
 * the image is plain MIP with extra arithmetic.
 */
const SELECTION_REPLACEMENT = [
    '      vec4 currentValue = getTextureValue(posIS);',
    '      float yggDensity = currentValue.r;',
    '      float yggAttenuated = yggDensity * yggTransmittance;',
    '      if (yggAttenuated > yggBestAttenuated) {',
    '        yggBestAttenuated = yggAttenuated;',
    '        selectedValue = vec4(currentValue.rgb * yggTransmittance, currentValue.a);',
    '        selectedPosVC = posVC;',
    '        selectedPosIS = posIS;',
    '      }',
    '      yggTransmittance *= exp(-yggDensity * yggExtinction);',
].join('\n');

/**
 * The `ShaderReplacements` array to hand a vtk.js volume mapper.
 *
 * @param {object} [options]
 * @param {number} [options.extinction] per-unit-density extinction.
 * @returns {object[]} ready for `mapper.setViewSpecificProperties({OpenGL: {ShaderReplacements}})`.
 */
export function attenuatedMipReplacements({ extinction = DEFAULT_EXTINCTION } = {}) {
    if (!Number.isFinite(extinction) || extinction < 0) {
        throw new Error(
            `extinction must be a non-negative finite number, got ${JSON.stringify(extinction)}. ` +
                'A negative value would amplify with depth rather than attenuate.'
        );
    }
    return [
        {
            shaderType: 'Fragment',
            originalValue: DECLARATION_ANCHOR,
            replacementValue: declarations(extinction),
            replaceFirst: false,
            replaceAll: false,
        },
        {
            shaderType: 'Fragment',
            originalValue: SELECTION_ANCHOR,
            replacementValue: SELECTION_REPLACEMENT,
            replaceFirst: false,
            // Both occurrences, deliberately -- see SELECTION_ANCHOR.
            replaceAll: true,
        },
    ];
}

/**
 * Refuse to proceed if vtk.js has moved the anchors.
 *
 * The failure this exists for is silent: `ShaderProgram.substitute` on a string that is
 * not present returns the source unchanged and reports nothing upward, so a vtk.js bump
 * would leave every volume render as plain MIP — dimmer depth cues gone, no error, and
 * the difference only visible to someone who knew what the old one looked like.
 *
 * Call it once at start-up with the real shader source.
 *
 * @param {string} shaderSource `vtkVolumeFS`.
 * @throws {Error} naming the anchor that moved.
 */
export function assertAnchorsPresent(shaderSource) {
    if (typeof shaderSource !== 'string' || shaderSource.length === 0) {
        throw new Error('The vtk.js volume fragment shader source is required.');
    }

    const problems = [];
    const declarationCount = occurrences(shaderSource, DECLARATION_ANCHOR);
    if (declarationCount !== 1) {
        problems.push(
            `the declaration anchor occurs ${declarationCount} times, expected exactly 1`
        );
    }
    const selectionCount = occurrences(shaderSource, SELECTION_ANCHOR);
    if (selectionCount !== SELECTION_ANCHOR_COUNT) {
        problems.push(
            `the selection anchor occurs ${selectionCount} times, expected ${SELECTION_ANCHOR_COUNT} ` +
                '(the sample loop and the residual final step)'
        );
    }

    if (problems.length) {
        throw new Error(
            'The vtk.js volume fragment shader no longer matches the anchors the ' +
                'attenuated-MIP replacement is written against:\n  ' +
                problems.join('\n  ') +
                '\nRe-derive them from the shipped shader. Until then the volume render ' +
                'would silently fall back to plain MIP, which is a visible clinical ' +
                'change with no error attached (findings F7 and F18).'
        );
    }
    return true;
}

/**
 * Apply the replacements to a source string, the way vtk.js will.
 *
 * Exists so the port can be tested as the string operation it is, without a GPU --
 * exactly how `niivue_render_modes.test.js` tests the splice it replaces.
 *
 * @param {string} shaderSource
 * @param {object[]} replacements
 * @returns {string}
 */
export function applyReplacements(shaderSource, replacements) {
    return replacements.reduce((source, replacement) => {
        if (replacement.replaceAll) {
            return source.split(replacement.originalValue).join(replacement.replacementValue);
        }
        const index = source.indexOf(replacement.originalValue);
        if (index < 0) {
            return source;
        }
        return (
            source.slice(0, index) +
            replacement.replacementValue +
            source.slice(index + replacement.originalValue.length)
        );
    }, shaderSource);
}

function occurrences(haystack, needle) {
    return haystack.split(needle).length - 1;
}

/** GLSL has no integer-to-float coercion in a `const float`, so `0` must be `0.0`. */
function formatFloat(value) {
    const text = String(value);
    return text.includes('.') || text.includes('e') ? text : `${text}.0`;
}
