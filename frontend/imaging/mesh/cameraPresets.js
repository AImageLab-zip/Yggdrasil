/**
 * The seven camera positions the IOS viewer offers, as data.
 *
 * Ported from `static/js/modality_viewers/ios.js:1080-1162`. Pure: a preset plus a
 * distance produces `{position, focalPoint, viewUp}`, which is exactly what
 * `viewport.setCamera` takes. Nothing here touches a viewport.
 *
 * ## Why the camera is set explicitly rather than reset
 *
 * `Viewport.resetCamera` looks like the obvious way to frame a mesh, and it is not:
 * for `ViewportType.VOLUME_3D` it multiplies the bounds radius by **ten** before placing
 * the camera (`RenderingEngine/Viewport.js`, the `radius` term), which frames a jaw scan
 * as a speck. It is fine for computing *bounds*; it is not a framing primitive here. So
 * the distance is derived from the bounds and the position set outright -- which the
 * presets have to do anyway.
 *
 * ## The odd `viewUp` vectors are deliberate
 *
 * Four presets use `[0, 0, 1]`. `upper` uses `[0, 1, 1]` and `lower` `[0, -1, -1]` --
 * not unit vectors, and at 45 degrees to the others. That is what the legacy viewer did,
 * it is what clinicians have been reading these scans against for years, and vtk
 * normalises `viewUp` itself, so the only effect of "fixing" them would be to rotate two
 * views by 45 degrees for no reason anybody asked for. They are transcribed, not
 * corrected.
 *
 * `upper` and `lower` also hide the opposite arch, which is a view of one jaw rather than
 * a camera angle on both. That is carried in the preset as `shows`.
 *
 * ## Every preset carries the legacy viewer's 180-degree Y rotation
 *
 * `ios.js:368` and `:394` set `mesh.rotation.y = Math.PI` on **each arch** before adding
 * it to the scene, and the camera vectors transcribed below were written against that
 * rotated scene. The Cornerstone port kept the cameras and dropped the rotation -- the
 * arches must not be transformed here, because a landmark is stored as a raw STL vertex
 * coordinate and `vtkCellPicker` reports world positions (see the entry's header). So
 * every view came out 180 degrees about Y from what clinicians read, which presents as
 * the upper and lower arches being swapped top for bottom.
 *
 * Rotating the *camera* by the same 180 degrees is visually identical and touches no
 * coordinate: a scene rotated by R and viewed from `(p, up)` looks exactly like an
 * unrotated scene viewed from `(R⁻¹p, R⁻¹up)`, and a 180-degree rotation is its own
 * inverse. So each preset below is the legacy vector with `Rᵧ(180): (x, y, z) →
 * (-x, y, -z)` applied to both its `direction` and its `viewUp`. `LEGACY_CAMERA_PRESETS`
 * keeps the untransformed numbers beside them so the two can be checked against each
 * other rather than remembered.
 */

/**
 * `Rᵧ(180)`: the rotation the legacy viewer applied to the meshes, applied to a vector.
 *
 * @param {number[]} vector
 * @returns {number[]}
 */
export function rotatedHalfTurnAboutY([x, y, z]) {
    // `-0` rather than `0` is what a bare negation produces for a zero component. It is
    // harmless to vtk and not harmless to `assert.deepStrictEqual`, which distinguishes
    // the two -- so a preset table would compare unequal to the obvious literal.
    const negate = (value) => (value === 0 ? 0 : -value);
    return [negate(x), y, negate(z)];
}

/**
 * The presets exactly as `static/js/modality_viewers/ios.js` set them, before the
 * half-turn. Kept so {@link CAMERA_PRESETS} can be *derived* rather than re-typed, and so
 * a test can assert the relationship instead of a table of hand-rotated numbers.
 */
export const LEGACY_CAMERA_PRESETS = Object.freeze({
    reset: { direction: [0, 1, 0], viewUp: [0, 0, -1], shows: ['upper', 'lower'] },
    front: { direction: [0, 1, 0], viewUp: [0, 0, -1], shows: null },
    right: { direction: [-1, 0, 0], viewUp: [0, 0, -1], shows: null },
    left: { direction: [1, 0, 0], viewUp: [0, 0, -1], shows: null },
    upper: { direction: [0, 0, 1], viewUp: [0, 1, -1], shows: ['upper'] },
    lower: { direction: [0, 0, -1], viewUp: [0, -1, 1], shows: ['lower'] },
});

/** The scene is centred on the origin: the meshes are their own frame and are not moved. */
export const FOCAL_POINT = Object.freeze([0, 0, 0]);

/**
 * `direction` is a unit vector from the focal point toward the camera; the caller scales
 * it by a distance derived from the mesh bounds. `shows` is the set of arches a preset
 * makes visible, or null to leave visibility alone.
 */
export const CAMERA_PRESETS = Object.freeze(
    Object.fromEntries(
        Object.entries(LEGACY_CAMERA_PRESETS).map(([name, preset]) => [
            name,
            Object.freeze({
                direction: Object.freeze(rotatedHalfTurnAboutY(preset.direction)),
                viewUp: Object.freeze(rotatedHalfTurnAboutY(preset.viewUp)),
                shows: preset.shows ? Object.freeze([...preset.shows]) : null,
            }),
        ])
    )
);

/** The legacy viewer's opening camera, at `(0, 80, 0)`. */
export const DEFAULT_DISTANCE = 80;

export function presetNames() {
    return Object.keys(CAMERA_PRESETS);
}

/**
 * A camera for one preset.
 *
 * @param {string} name
 * @param {number} [distance] how far from the focal point; defaults to the legacy 80.
 * @returns {{position: number[], focalPoint: number[], viewUp: number[]}|null}
 */
export function cameraFor(name, distance = DEFAULT_DISTANCE) {
    const preset = CAMERA_PRESETS[name];
    if (!preset) return null;
    const span = Number.isFinite(distance) && distance > 0 ? distance : DEFAULT_DISTANCE;
    return {
        position: preset.direction.map((component) => component * span),
        focalPoint: [...FOCAL_POINT],
        viewUp: [...preset.viewUp],
    };
}

/** Which arches a preset makes visible, or null when it leaves visibility alone. */
export function visibilityFor(name) {
    const preset = CAMERA_PRESETS[name];
    return preset?.shows ? [...preset.shows] : null;
}

/**
 * A viewing distance that fits the meshes, from vtk bounds.
 *
 * The bounding sphere's radius times a margin. Half the *diagonal* rather than half the
 * largest side, so a jaw seen from the front -- wide and shallow -- is not cropped when
 * the camera swings round to the side.
 *
 * @param {number[]} bounds `[xMin, xMax, yMin, yMax, zMin, zMax]`.
 */
export function distanceForBounds(bounds, margin = 2.2) {
    if (!Array.isArray(bounds) || bounds.length !== 6 || !bounds.every(Number.isFinite)) {
        return DEFAULT_DISTANCE;
    }
    const [xMin, xMax, yMin, yMax, zMin, zMax] = bounds;
    const diagonal = Math.hypot(xMax - xMin, yMax - yMin, zMax - zMin);
    if (!(diagonal > 0)) return DEFAULT_DISTANCE;
    return (diagonal / 2) * margin;
}


/**
 * The bounding sphere's radius, in the meshes' own units.
 *
 * Separate from {@link distanceForBounds}, which multiplies it by a viewing margin. The
 * axes want the scans' actual size, not how far away the camera sits -- conflating the two
 * is how the reference axes first shipped several times longer than the jaws.
 *
 * @param {number[]} bounds `[xMin, xMax, yMin, yMax, zMin, zMax]`.
 */
export function radiusOf(bounds) {
    if (!Array.isArray(bounds) || bounds.length !== 6 || !bounds.every(Number.isFinite)) {
        return 0;
    }
    const [xMin, xMax, yMin, yMax, zMin, zMax] = bounds;
    return Math.hypot(xMax - xMin, yMax - yMin, zMax - zMin) / 2;
}
