/**
 * The three volume-render modes the CBCT toolbar offers, mapped onto vtk.js.
 *
 * `templates/maxillo/patient_detail_content.html` has offered `mip`, `amip` and
 * `shaded` since long before this migration, with **`amip` selected by default**. That
 * default is the reason this module is not a two-line lookup: `mip` and `shaded` have
 * clean vtk.js equivalents and `amip` does not (finding F7), so it arrives as a blend
 * mode *plus* a shader replacement (F18, `attenuatedMip.js`).
 *
 * Pure: it returns a description of what to apply. The viewport call is at the call
 * site, which is what lets the mapping — including the one entry that is easy to get
 * subtly wrong — be tested without a GPU.
 */

import { attenuatedMipReplacements } from './attenuatedMip.js';

/**
 * vtk.js `BlendMode` values, inlined from
 * `@kitware/vtk.js/Rendering/Core/VolumeMapper/Constants.js`.
 *
 * Checked by {@link assertBlendModesMatch} rather than trusted, for the same reason
 * `layout.js` checks its enum copy: a renamed constant would silently select a
 * different projection instead of failing.
 */
export const BLEND_MODES = Object.freeze({
    COMPOSITE_BLEND: 0,
    MAXIMUM_INTENSITY_BLEND: 1,
});

/** The values the `<select id="cbctRenderMode">` offers, in template order. */
export const RENDER_MODES = Object.freeze(['mip', 'amip', 'shaded']);

/** What the toolbar shows for each, matching the existing `<option>` labels. */
export const RENDER_MODE_LABELS = Object.freeze({
    mip: 'MIP',
    amip: 'Attenuated MIP',
    shaded: 'Shaded Volume',
});

/** The mode the template marks `selected`, and therefore what most users look at. */
export const DEFAULT_RENDER_MODE = 'amip';

/**
 * Describe how to configure a volume mapper and property for one mode.
 *
 * @param {string} mode one of {@link RENDER_MODES}.
 * @returns {{
 *   blendMode: number,
 *   shaderReplacements: object[],
 *   shade: boolean,
 *   ambient: number, diffuse: number, specular: number,
 *   label: string
 * }}
 */
export function renderModeSpec(mode) {
    switch (mode) {
        case 'mip':
            return {
                blendMode: BLEND_MODES.MAXIMUM_INTENSITY_BLEND,
                shaderReplacements: [],
                // Lighting is meaningless under a maximum-intensity projection: the
                // output is one sampled value per ray, not an accumulated surface.
                // Leaving shading on would cost the gradient computation for nothing.
                shade: false,
                ambient: 1,
                diffuse: 0,
                specular: 0,
                label: RENDER_MODE_LABELS.mip,
            };

        case 'amip':
            return {
                // Still a maximum-intensity projection -- the attenuation changes *what*
                // the maximum is taken over, not the projection. The replacements only
                // substitute inside the MIP branch of the shader, so any other blend
                // mode would silently leave them compiled out (see attenuatedMip.js).
                blendMode: BLEND_MODES.MAXIMUM_INTENSITY_BLEND,
                shaderReplacements: attenuatedMipReplacements(),
                shade: false,
                ambient: 1,
                diffuse: 0,
                specular: 0,
                label: RENDER_MODE_LABELS.amip,
            };

        case 'shaded':
            return {
                blendMode: BLEND_MODES.COMPOSITE_BLEND,
                shaderReplacements: [],
                shade: true,
                ambient: 0.3,
                diffuse: 0.7,
                specular: 0.2,
                label: RENDER_MODE_LABELS.shaded,
            };

        default:
            throw new Error(
                `Unknown render mode '${mode}'. Expected one of ${RENDER_MODES.join(', ')}.`
            );
    }
}

/**
 * Apply a mode to a vtk.js volume actor.
 *
 * The shader replacements go on **before** the blend mode, because
 * `getNeedToRebuildShaders` includes both in the state it compares
 * (`Rendering/OpenGL/VolumeMapper.js:185-224`); setting the blend mode last means the
 * rebuild it triggers already sees the replacements. The reverse order works today and
 * depends on the rebuild being triggered twice, which is not something to rely on.
 *
 * @param {object} actor a vtk volume actor.
 * @param {string} mode
 * @returns {object} the spec that was applied.
 */
export function applyRenderMode(actor, mode) {
    const spec = renderModeSpec(mode);
    const mapper = actor.getMapper();
    const property = actor.getProperty();

    mapper.setViewSpecificProperties({
        ...(mapper.getViewSpecificProperties() || {}),
        OpenGL: { ShaderReplacements: spec.shaderReplacements },
    });
    mapper.setBlendMode(spec.blendMode);

    property.setShade(spec.shade);
    property.setAmbient(spec.ambient);
    property.setDiffuse(spec.diffuse);
    property.setSpecular(spec.specular);

    return spec;
}

/**
 * Check the inlined blend-mode numbers against vtk.js's own.
 *
 * @param {object} blendModeEnum `@kitware/vtk.js` `BlendMode`.
 * @throws {Error} naming every value that no longer matches.
 */
export function assertBlendModesMatch(blendModeEnum) {
    const problems = Object.entries(BLEND_MODES)
        .filter(([name, value]) => blendModeEnum?.[name] !== value)
        .map(([name, value]) => `${name}: expected ${value}, got ${blendModeEnum?.[name]}`);

    if (problems.length) {
        throw new Error(
            'vtk.js BlendMode values have changed under frontend/imaging/grid/renderModes.js:\n  ' +
                problems.join('\n  ') +
                '\nA wrong number here selects a different projection without failing.'
        );
    }
    return true;
}
