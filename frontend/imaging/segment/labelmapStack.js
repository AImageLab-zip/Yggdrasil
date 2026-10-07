/**
 * The labels on screen: one Cornerstone *segmentation* per label.
 *
 * ## Why one segmentation per label, not one with several segments
 *
 * Labels may overlap, and a Cornerstone labelmap holds one value per pixel. Cornerstone
 * 5.8 can split a single segmentation into private layers when `overwriteMode: 'none'`,
 * and painting overlapping labels that way works -- but the layer split is *reactive*: it
 * happens when a stroke collides, and there is no supported way to restore a saved overlap
 * into layers up front. One segmentation per label has none of that: each label owns a
 * labelmap image that **is** its plane, so loading is a buffer write, saving is a buffer
 * read, painting one label can never overwrite another, and undo is per stroke. The cost
 * is one `W*H` byte buffer per label, which a plane would cost anyway.
 *
 * Measured on a stack viewport: paint l1, paint l2 over it (both keep 9976 px), undo and
 * redo restore exactly, erase touches only the active label, and per-label visibility and
 * opacity apply without disturbing the others.
 *
 * Cornerstone is injected. Nothing here persists an id: segmentation ids are runtime
 * handles built from the label's *code*, which is what the server stores.
 */

import { planeFromLabelmap, writePlaneToLabelmap } from './planes.js';

/** The one segment every label's labelmap uses. */
export const SEGMENT_INDEX = 1;

/** `#rrggbb` -> `[r, g, b]`. */
export function hexToRgb(hex) {
    const match = /^#([0-9a-f]{6})$/i.exec(hex ?? '');
    if (!match) {
        throw new Error(`Not a #rrggbb colour: ${hex}`);
    }
    const value = parseInt(match[1], 16);
    return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

/** A 256-entry colour LUT in which only segment 1 is visible, in the label's colour. */
export function buildColorLut(hex) {
    const lut = new Array(256).fill(null).map(() => [0, 0, 0, 0]);
    lut[SEGMENT_INDEX] = [...hexToRgb(hex), 255];
    return lut;
}

export function segmentationIdFor(prefix, code) {
    return `${prefix}:${code}`;
}

/**
 * @param {object} options
 * @param {object} options.cornerstone `{imageLoader, cache, segmentation, toolsEnums}`
 * @param {string} options.viewportId
 * @param {string} options.imageId the frame's image id.
 * @param {string} options.prefix unique per mount, so two mounts cannot share a segmentation.
 */
export function createLabelmapStack({ cornerstone, viewportId, imageId, prefix }) {
    const { imageLoader, cache, segmentation, toolsEnums } = cornerstone;
    const REPRESENTATION = toolsEnums.SegmentationRepresentations.Labelmap;

    /** code -> {segmentationId, labelmapImageId, lutIndex} */
    const labels = new Map();
    let activeCode = null;

    function pixelsOf(entry) {
        return cache.getImage(entry.labelmapImageId).voxelManager.getScalarData();
    }

    function requireLabel(code) {
        const entry = labels.get(code);
        if (!entry) {
            throw new Error(`No label ${code} is mounted`);
        }
        return entry;
    }

    const stack = {
        has: (code) => labels.has(code),
        codes: () => [...labels.keys()],
        get activeCode() {
            return activeCode;
        },

        /** Mount a label: its labelmap image, its segmentation, and its representation. */
        async addLabel({ code, color }) {
            if (labels.has(code)) {
                return;
            }
            const [derived] = imageLoader.createAndCacheDerivedLabelmapImages([imageId]);
            const segmentationId = segmentationIdFor(prefix, code);
            segmentation.addSegmentations([
                {
                    segmentationId,
                    representation: {
                        type: REPRESENTATION,
                        data: { imageIds: [derived.imageId] },
                    },
                    // Declared even for one segment: an undeclared segment cannot be
                    // controlled, and cannot even be hidden.
                    config: { segments: { [SEGMENT_INDEX]: { label: code, active: true } } },
                },
            ]);
            const lutIndex = segmentation.config.color.addColorLUT(buildColorLut(color));
            await segmentation.addSegmentationRepresentations(viewportId, [
                { segmentationId, type: REPRESENTATION },
            ]);
            segmentation.config.color.setColorLUT(viewportId, segmentationId, lutIndex);
            labels.set(code, { segmentationId, labelmapImageId: derived.imageId, lutIndex });
        },

        /** Unmount a label. Its pixels go with it; the saved plane is what survives. */
        removeLabel(code) {
            const entry = labels.get(code);
            if (!entry) {
                return;
            }
            segmentation.removeSegmentationRepresentations(viewportId, {
                segmentationId: entry.segmentationId,
                type: REPRESENTATION,
            });
            segmentation.removeSegmentation(entry.segmentationId);
            labels.delete(code);
            if (activeCode === code) {
                activeCode = null;
            }
        },

        /** Which label the brush and shape tools write into. */
        setActive(code) {
            const entry = requireLabel(code);
            segmentation.activeSegmentation.setActiveSegmentation(viewportId, entry.segmentationId);
            segmentation.segmentIndex.setActiveSegmentIndex(entry.segmentationId, SEGMENT_INDEX);
            activeCode = code;
        },

        setVisible(code, visible) {
            const entry = requireLabel(code);
            segmentation.config.visibility.setSegmentationRepresentationVisibility(
                viewportId,
                { segmentationId: entry.segmentationId },
                Boolean(visible)
            );
        },

        /** @param {number} alpha 0..1 */
        setOpacity(code, alpha) {
            const entry = requireLabel(code);
            const fillAlpha = Math.min(1, Math.max(0, alpha));
            segmentation.config.style.setStyle(
                { viewportId, segmentationId: entry.segmentationId, type: REPRESENTATION },
                { fillAlpha, fillAlphaInactive: fillAlpha }
            );
        },

        setColor(code, color) {
            const entry = requireLabel(code);
            segmentation.config.color.addColorLUT(buildColorLut(color), entry.lutIndex);
            segmentation.config.color.setColorLUT(viewportId, entry.segmentationId, entry.lutIndex);
        },

        /** A copy of the label's mask as a 0/1 plane. */
        readPlane(code) {
            return planeFromLabelmap(pixelsOf(requireLabel(code)));
        },

        /** Replace the label's mask. Does not enter undo history: it is a load, not an edit. */
        writePlane(code, plane) {
            const entry = requireLabel(code);
            writePlaneToLabelmap(pixelsOf(entry), plane, SEGMENT_INDEX);
            segmentation.triggerSegmentationEvents.triggerSegmentationDataModified(
                entry.segmentationId
            );
        },

        /** Which label owns a segmentation id, or null: how an edit event finds its label. */
        codeForSegmentation(segmentationId) {
            for (const [code, entry] of labels) {
                if (entry.segmentationId === segmentationId) {
                    return code;
                }
            }
            return null;
        },

        destroy() {
            for (const code of [...labels.keys()]) {
                stack.removeLabel(code);
            }
        },
    };
    return stack;
}
