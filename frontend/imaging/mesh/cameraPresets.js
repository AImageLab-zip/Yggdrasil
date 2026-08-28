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
 * Five presets use `[0, 0, -1]`. `viewUpper` uses `[0, 1, -1]` and `viewLower`
 * `[0, -1, 1]` -- not unit vectors, and at 45 degrees to the others. That is what the
 * legacy viewer did, it is what clinicians have been reading these scans against for
 * years, and vtk normalises `viewUp` itself, so the only effect of "fixing" them would be
 * to rotate two views by 45 degrees for no reason anybody asked for. They are transcribed,
 * not corrected.
 *
 * `viewUpper` and `viewLower` also hide the opposite arch, which is a view of one jaw
 * rather than a camera angle on both. That is carried in the preset as `shows`.
 */

/** The scene is centred on the origin: the meshes are their own frame and are not moved. */
export const FOCAL_POINT = Object.freeze([0, 0, 0]);

/**
 * `direction` is a unit vector from the focal point toward the camera; the caller scales
 * it by a distance derived from the mesh bounds. `shows` is the set of arches a preset
 * makes visible, or null to leave visibility alone.
 */
export const CAMERA_PRESETS = Object.freeze({
    reset: { direction: [0, 1, 0], viewUp: [0, 0, -1], shows: ['upper', 'lower'] },
    front: { direction: [0, 1, 0], viewUp: [0, 0, -1], shows: null },
    right: { direction: [-1, 0, 0], viewUp: [0, 0, -1], shows: null },
    left: { direction: [1, 0, 0], viewUp: [0, 0, -1], shows: null },
    upper: { direction: [0, 0, 1], viewUp: [0, 1, -1], shows: ['upper'] },
    lower: { direction: [0, 0, -1], viewUp: [0, -1, 1], shows: ['lower'] },
});

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
