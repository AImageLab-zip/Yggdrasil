/**
 * Which spheres to draw, where, and in what colour.
 *
 * Pure: a document plus some visibility state produces a list of descriptors. It builds no
 * actors, so the whole marker layer is testable with `node --test` -- and, more to the
 * point, so is the claim the entire migration rests on.
 *
 * ## The claim: a marker sits at the stored coordinates, untransformed
 *
 * A landmark is stored in `resource_local` coordinates -- the STL's own object space. The
 * legacy viewer rotated each jaw by 180 degrees about Y and translated both by the negated
 * centre of their combined bounding box, then stored
 * `mesh.worldToLocal(hit.point)` and drew markers back at `mesh.localToWorld(stored)`.
 * Because `worldToLocal` inverts the *full* world matrix, those two transforms cancel: the
 * stored numbers are raw STL vertex coordinates.
 *
 * `@cornerstonejs/core`'s `Mesh` applies no transform to an STL actor, so if the marker
 * actors are also left untransformed, `position === stored` and the two eras agree exactly.
 * That is why {@link markersFor} copies the point through and does nothing else to it, and
 * why `frontend/tests/meshLandmarks.test.js` asserts both that identity and that the
 * upstream class still applies no transform. A version bump that started centring meshes
 * would otherwise move every landmark on every historical study, silently, and look fine.
 *
 * ## Visibility is three conditions, ANDed
 *
 * Carried over from `syncLandmarkVisibility` (`ios.js:890-900`): the workbench must be open
 * or the eye toggled on, the owning arch's mesh must be visible, and the type must not be
 * switched off. The middle one is the one that is easy to miss and obvious when wrong --
 * hiding the upper jaw has to hide its landmarks, or they float in space over the lower.
 */

import {
    LANDMARK_TYPES,
    TYPE_COLORS,
    isMultiPoint,
    landmarks,
    sameLandmark,
} from './landmarkDocument.js';

/** The legacy marker radius, in millimetres of the mesh's own frame. */
export const DEFAULT_MARKER_SIZE = 0.65;

/** A selected marker is drawn larger and fully opaque. */
export const SELECTED_SCALE = 1.45;

export const DEFAULT_OPACITY = 0.92;

/**
 * The markers to draw.
 *
 * @param {object} document the landmark document.
 * @param {object} state
 * @param {boolean} state.visible whether landmarks are shown at all.
 * @param {object} state.jawVisible `{upper: boolean, lower: boolean}`.
 * @param {object} [state.typeVisible] per-type overrides; absent reads as visible.
 * @param {object} [state.selected] the selected landmark's identity.
 * @param {number} [state.markerSize]
 * @returns {object[]} `{uid, position, radius, color, opacity, jaw, tooth, type, index,
 *   selected}` -- `position` is the stored point, unmodified.
 */
export function markersFor(document, {
    visible = true,
    jawVisible = { upper: true, lower: true },
    typeVisible = {},
    selected = null,
    markerSize = DEFAULT_MARKER_SIZE,
} = {}) {
    if (!visible) return [];
    return landmarks(document)
        .filter((landmark) => jawVisible?.[landmark.jaw] !== false)
        .filter((landmark) => typeVisible?.[landmark.type] !== false)
        .map((landmark) => {
            const isSelected = sameLandmark(selected, landmark);
            return {
                uid: markerUid(landmark),
                // Untransformed, and that is the whole compatibility story. See the header.
                position: [...landmark.point],
                radius: markerSize * (isSelected ? SELECTED_SCALE : 1),
                color: TYPE_COLORS[landmark.type] ?? 0xffffff,
                opacity: isSelected ? 1 : DEFAULT_OPACITY,
                jaw: landmark.jaw,
                tooth: landmark.tooth,
                type: landmark.type,
                index: landmark.index,
                selected: isSelected,
            };
        });
}

/**
 * A stable id for one marker.
 *
 * Stable across redraws so a caller can diff two marker lists instead of tearing the whole
 * layer down -- and derived from the landmark's own identity rather than a counter, which
 * would renumber everything when a landmark earlier in the order is deleted.
 */
export function markerUid({ jaw, tooth, type, index }) {
    return `${jaw}:${tooth}:${type}:${index ?? 0}`;
}

/** `#rrggbb` for the workbench swatches; the actors take the packed integer. */
export function cssColor(type) {
    const value = TYPE_COLORS[type] ?? 0xffffff;
    return `#${value.toString(16).padStart(6, '0')}`;
}

/**
 * Which landmark a picked marker actor is.
 *
 * The viewport keeps `uid -> landmark`; this is the lookup, here rather than there so the
 * selection round-trip can be tested without a picker.
 */
export function landmarkForUid(document, uid) {
    return landmarks(document).find((landmark) => markerUid(landmark) === uid) ?? null;
}

/** Per-type counts, for the visibility dropdown's badges. */
export function countsByType(document) {
    const counts = Object.fromEntries(LANDMARK_TYPES.map((type) => [type, 0]));
    for (const landmark of landmarks(document)) {
        counts[landmark.type] += 1;
    }
    return counts;
}

/** Whether a type can have more than one point per tooth. Re-exported for the workbench. */
export { isMultiPoint };
