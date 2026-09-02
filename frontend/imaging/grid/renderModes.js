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
 * How the segmentation's own actor renders, which is **not** the study's mode.
 *
 * The 3D window holds two volume actors: the study and, once the overlay is on, the
 * labelmap. Applying the study's mode to both was the previous round's fix for a real
 * problem -- the two were projected differently and the result was a haze -- and it
 * produced a different wrong picture, which is what the screenshots show: an attenuated
 * *maximum-intensity* projection through a labelmap takes the largest label value along
 * each ray, so the tooth with the highest number wins regardless of which is in front,
 * and the result is a flat translucent map rather than solid structures in depth.
 *
 * A labelmap is not an intensity field and must not be projected like one. It is
 * composited, front to back, so a voxel that is in front is in front. Shading is on for
 * the same reason: a composite of one flat colour is a silhouette, and a segmentation
 * whose crowns and roots cannot be told apart is not showing anatomy.
 *
 * The *other* half of "coloured voxels" is Cornerstone's own labelmap style, which
 * defaults to a 3px outline over a 50% fill (`labelmapConfig.js`) -- correct on a slice,
 * and on a volume render exactly the outlined shells that were reported. See
 * `solidVoxelStyle` in `imaging/grid/segmentation.js`.
 *
 * ## Ambient is 1, and that is the whole of why this stopped rendering black
 *
 * **A labelmap has no gradient to shade with, anywhere except its own surface.** vtk's
 * volume shader computes a normal by central difference and gives up when there is none
 * (`Rendering/OpenGL/glsl/vtkVolumeFS.glsl`):
 *
 * ```glsl
 * result.w = length(result.xyz);
 * if (result.w == 0.0) { return vec4(0.0); }
 * ```
 *
 * A label's interior is piecewise constant, so the difference is exactly zero and the
 * normal comes back as `vec4(0.0)`. `applyLighting` then computes
 * `df = dot(normal, lightDirection)`, which is `0.0` and therefore not `> 0.0`, so the
 * diffuse term never accumulates and the sample is left as
 *
 * ```glsl
 * tColor * (diffuse * volume.diffuse + volume.ambient)  //  ->  tColor * ambient
 * ```
 *
 * With `ambient: 0.3` that is **every interior voxel at 30% of its colour**, composited
 * over the depth of the structure. The segmentation rendered dark, and the palette this
 * module reproduces value-for-value from the NiiVue viewer was being multiplied by 0.3
 * on its way to the screen -- a tooth specified `#38d66b` arrived as `#113f20`. It also
 * explains the *timing* that was reported: the first frame is drawn from Cornerstone's
 * own actor property, and the picture goes dark a moment later when
 * `viewportManager.setRenderMode` applies this spec.
 *
 * So `ambient: 1` -- the floor is the colour the palette asked for, and nothing can make
 * a voxel darker than the label it belongs to. `diffuse` stays, at a quarter, because the
 * one place a labelmap *does* have a gradient is the boundary shell, which is the surface
 * a reader is looking at: it lands as a lit rim on the faces turned towards the camera,
 * which is the depth cue the shading was turned on for in the first place. It is additive
 * over a floor of 1, so a fully-lit rim clips about 25% high and reads as a highlight
 * rather than as a colour shift. `specular` is 0: a second additive term on top of that
 * clips further and says nothing a labelmap can support.
 */
export const LABELMAP_RENDER_SPEC = Object.freeze({
    blendMode: BLEND_MODES.COMPOSITE_BLEND,
    shaderReplacements: Object.freeze([]),
    shade: true,
    ambient: 1,
    diffuse: 0.25,
    specular: 0,
    label: 'Segmentation',
});

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
    return applySpec(actor, renderModeSpec(mode));
}

/**
 * Put one actor onto {@link LABELMAP_RENDER_SPEC}.
 *
 * @param {object} actor a vtk volume actor carrying a labelmap.
 * @returns {object} the spec that was applied.
 */
export function applyLabelmapRenderMode(actor) {
    return applySpec(actor, LABELMAP_RENDER_SPEC);
}

function applySpec(actor, spec) {
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
