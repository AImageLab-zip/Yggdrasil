/**
 * The segmentation overlay on the volume grid: brain tumour classes and CBCT teeth.
 *
 * **What was lost, and why this exists.** Before 3.0 the overlay was free: NiiVue took
 * a second volume with `nv.loadFromArrayBuffer`, gave it a colormap, and composited it
 * into every slice type -- including the 3D render -- with no further code. `c03afa6`
 * deleted that viewer and did not replace this part, so `viewer_grid_data.segmentationFile`
 * has been emitted by both `brain/views.py` and `maxillo/views/patient_detail.py` and
 * read by nobody since. Cornerstone gives no such free ride: a labelmap is explicit
 * state with an explicit representation per viewport.
 *
 * **One labelmap, every viewport, the 3D one included.** The first version of this
 * module gave the `volume3d` window a *Surface* representation instead, on the belief
 * that a 3D viewport cannot render a labelmap. It can:
 * `getViewportLabelmapRenderMode` returns `'volume'` for anything extending
 * `BaseVolumeViewport`, and `VolumeViewport3D` does, so the labelmap becomes a second
 * volume actor with its own transfer function -- which is precisely what NiiVue was
 * doing. The surface route was worse in every respect that matters here: it needs the
 * polySeg add-on registered, it extracts one mesh per label in a web worker, and each
 * extraction calls `getCompleteScalarDataArray()`, which allocates *a fresh copy of the
 * whole volume* (`VoxelManager.js:649`). For a CBCT carrying thirty-odd teeth that is
 * thirty-odd copies of a 10^8-voxel volume. It rendered nothing and cost a great deal
 * to do it.
 *
 * Three things this module is careful about, each of which is a way to be wrong that
 * looks right on screen:
 *
 *   1. **A grid mismatch is refused, not resampled.** A labelmap describes voxels of
 *      one volume. If the segmentation's dimensions are not the reference volume's,
 *      the two are not the same study and any resampling would file a rendering guess
 *      as clinical data.
 *   2. **The palette is the old one, value for value.** Users approved work against
 *      those colours. `paletteFor` reproduces both branches of
 *      `static/js/modality_viewers/niivue_viewer.js:163-194` -- recover it with
 *      `git show c03afa6^:static/js/modality_viewers/niivue_viewer.js` -- so a class
 *      that was green stays green.
 *   3. **Only the labels actually present are offered.** A CBCT declares
 *      `labelMax: 98` (`maxillo/views/patient_detail.py:191`) and carries some thirty
 *      teeth. Listing 98 checkboxes, 66 of which control nothing, is not a class list.
 *
 * Cornerstone is injected rather than imported, like every other module under
 * `imaging/grid/`, so everything here except the two calls that need a GPU is testable
 * under `node --test`.
 */

import { BLEND_MODES } from './renderModes.js';
import { awaitVolumeLoad, readScalarData } from './volumeLoading.js';

/** Cornerstone `SegmentationRepresentations` values, inlined -- see `layout.js`. */
export const REPRESENTATIONS = Object.freeze({
    LABELMAP: 'Labelmap',
});

/** The one segmentation this grid shows. One study, one overlay. */
export const SEGMENTATION_ID = 'ygg-grid-segmentation';

/**
 * The volumes the overlay owns, which no window holds.
 *
 * `releaseUnusedVolumes` evicts everything not in a window, so without naming these it
 * would drop the labelmap and its source on the next drop and leave Cornerstone's
 * segmentation state pointing at volumes the cache no longer has.
 *
 * @param {string} [segmentationId]
 * @returns {string[]}
 */
export function ownedVolumeIds(segmentationId = SEGMENTATION_ID) {
    return [segmentationId, `${segmentationId}-source`];
}

/** What the overlay is drawn at. `viewer_grid.js:17` used the same value. */
export const SEGMENTATION_OPACITY = 0.5;

/**
 * Golden-ratio hue walk, as `niivue_viewer.js` computed it for `labelMax > 3`.
 *
 * Reproduced rather than replaced: it is what a CBCT's teeth have looked like, and a
 * "nicer" palette would silently recolour every stored segmentation in the archive.
 *
 * @param {number} value the label value.
 * @returns {number[]} `[r, g, b]`, each 0-255.
 */
export function goldenHue(value) {
    const hue = (value * 0.61803398875) % 1;
    const sector = Math.floor(hue * 6);
    const fraction = hue * 6 - sector;
    const p = 0.25;
    const q = 1 - fraction * 0.75;
    const t = 0.25 + fraction * 0.75;
    const rgb = [
        [1, t, p], [q, 1, p], [p, 1, t],
        [p, q, 1], [t, p, 1], [1, p, q],
    ][sector % 6];
    return rgb.map((channel) => Math.round(channel * 255));
}

/**
 * The three fixed colours the brain overlay has always used, by label value.
 *
 * `templates/brain/patient_detail_content.html`'s help modal still tells the user
 * "green, red, blue" in that order, so this is a documented contract as well as a
 * remembered one.
 */
export const BRAIN_COLOURS = Object.freeze([
    [0, 255, 0],   // 1
    [255, 0, 0],   // 2
    [0, 0, 255],   // 3
]);

/**
 * A Cornerstone colour LUT for the labels present, index 0 transparent.
 *
 * Cornerstone indexes a colour LUT by segment index, so the array has to be dense from
 * 0 up to the highest label -- a sparse map would colour segment 12 with segment 3's
 * entry. Absent labels get a transparent entry, which costs four bytes each and cannot
 * be seen.
 *
 * @param {number[]} labelValues the values actually present, any order.
 * @returns {number[][]} `[[r, g, b, a], ...]` indexed by segment index.
 */
export function paletteFor(labelValues) {
    const present = [...new Set(labelValues.map(Number))]
        .filter((value) => Number.isInteger(value) && value > 0)
        .sort((a, b) => a - b);
    if (present.length === 0) {
        return [[0, 0, 0, 0]];
    }

    // Three or fewer classes is the brain case, and the one the help text describes.
    const fixed = present.length <= BRAIN_COLOURS.length && present[present.length - 1] <= 3;
    const lut = [[0, 0, 0, 0]];
    for (let value = 1; value <= present[present.length - 1]; value += 1) {
        if (!present.includes(value)) {
            lut.push([0, 0, 0, 0]);
            continue;
        }
        const [r, g, b] = fixed ? BRAIN_COLOURS[value - 1] : goldenHue(value);
        lut.push([r, g, b, 255]);
    }
    return lut;
}

/**
 * The distinct non-zero values in a labelmap, and whether it has any at all.
 *
 * One pass over the scalar data, done while it is already in hand rather than as a
 * second traversal of sixty million voxels.
 *
 * @param {ArrayLike<number>} scalarData
 * @returns {number[]} sorted ascending.
 */
export function labelValuesIn(scalarData) {
    const present = new Set();
    for (let index = 0; index < scalarData.length; index += 1) {
        const value = scalarData[index];
        if (value !== 0) {
            present.add(value);
        }
    }
    return [...present].sort((a, b) => a - b);
}

/**
 * The `segments` map Cornerstone needs in order to know a label exists at all.
 *
 * Keyed by segment index, which for this platform *is* the label value: the labelmap
 * stores the label in the voxel and the default `labelToSegmentIndex` is the identity,
 * so segment 12 is label 12 and the colour LUT is indexed the same way.
 *
 * @param {number[]} labelValues
 * @returns {object}
 */
export function segmentsConfig(labelValues) {
    const config = {};
    for (const value of labelValues) {
        config[value] = {
            // No names are stored for these classes anywhere in the platform, and
            // inventing a table would put a clinical label on screen nothing can back
            // up. The value is what the record actually says.
            label: `Label ${value}`,
            active: value === labelValues[0],
        };
    }
    return config;
}

/**
 * Whether two volumes describe the same voxel grid.
 *
 * Dimensions and spacing, both: two volumes can agree on the number of voxels and
 * disagree on how big one is, and a labelmap laid over the wrong spacing is off by a
 * growing amount rather than uniformly, which reads as a segmentation that "drifts".
 *
 * @returns {string|null} null when they agree, else why they do not.
 */
export function gridMismatch(reference, segmentation) {
    const refDims = reference?.dimensions ?? [];
    const segDims = segmentation?.dimensions ?? [];
    if (refDims.length !== 3 || segDims.length !== 3) {
        return 'One of the volumes does not state its dimensions.';
    }
    if (refDims.some((value, axis) => value !== segDims[axis])) {
        return (
            `The segmentation is ${segDims.join('×')} voxels and the volume is ` +
            `${refDims.join('×')}. They are not the same study.`
        );
    }
    const refSpacing = reference?.spacing ?? [];
    const segSpacing = segmentation?.spacing ?? [];
    const drifted = refSpacing.some(
        (value, axis) => Math.abs(value - (segSpacing[axis] ?? 0)) > 1e-4
    );
    if (drifted) {
        return (
            `The segmentation's voxel size (${segSpacing.join(', ')} mm) does not match ` +
            `the volume's (${refSpacing.join(', ')} mm).`
        );
    }
    return null;
}

/**
 * Load a segmentation NIfTI and register it as a labelmap over a loaded volume.
 *
 * Returns `{ ok: false, reason }` rather than throwing for the two outcomes a user has
 * to be told about -- a grid mismatch, and a file with no labels in it. A genuine
 * failure (the fetch, the loader) still throws.
 *
 * @param {object} options
 * @param {object} options.cornerstone the injected bag, as `createVolumeGrid` receives it.
 * @param {string} options.referenceVolumeId the volume already in the viewports.
 * @param {string} options.url the segmentation's serve URL.
 * @param {string} [options.segmentationId]
 * @returns {Promise<{ok: boolean, reason?: string, labelValues?: number[]}>}
 */
export async function loadSegmentation({
    cornerstone,
    referenceVolumeId,
    url,
    segmentationId = SEGMENTATION_ID,
}) {
    const { volumeLoader, createNiftiImageIdsAndCacheMetadata, cache, segmentation } =
        cornerstone;

    const reference = cache.getVolume(referenceVolumeId);
    if (!reference) {
        return { ok: false, reason: 'This window has no volume for a segmentation to describe.' };
    }

    // Loaded the same way the greyscale volume is, so the loader-safety rules in
    // imaging/ids/imageIds.js apply to it identically.
    const sourceId = `${segmentationId}-source`;
    let source = cache.getVolume(sourceId);
    if (!source) {
        const imageIds = await createNiftiImageIdsAndCacheMetadata({ url });
        if (!imageIds?.length) {
            throw new Error('The loader produced no imageIds for this segmentation.');
        }
        source = await volumeLoader.createAndCacheVolume(sourceId, { imageIds });
        // As in loadVolumeIntoWindows: `volume.load()` returns undefined, so awaiting
        // it hands back a volume with no frames and a zero-length scalar array.
        await awaitVolumeLoad(source);
    }

    const mismatch = gridMismatch(reference, source);
    if (mismatch) {
        return { ok: false, reason: `Segmentation not shown. ${mismatch}` };
    }

    const scalarData = readScalarData(source);
    const labelValues = labelValuesIn(scalarData);
    if (labelValues.length === 0) {
        return { ok: false, reason: 'This segmentation is empty — every voxel is background.' };
    }

    // A *derived* labelmap rather than the loaded volume itself: Cornerstone expects a
    // labelmap to have been minted against its reference, and the derived volume
    // inherits the reference's geometry and metadata rather than carrying a second,
    // independently-parsed copy of it that could differ in the last decimal place.
    // Synchronous in 5.8.2 (`loaders/volumeLoader.js:229`): it allocates a Uint8Array
    // of the reference's shape and returns the volume, with nothing to fetch.
    const labelmap = volumeLoader.createAndCacheDerivedLabelmapVolume(
        referenceVolumeId,
        { volumeId: segmentationId }
    );
    // **One write, not one per voxel.** The obvious loop -- `voxelManager.setAtIndex` for
    // every non-zero voxel -- resolves the owning slice's voxel manager on each call and
    // marks that slice dirty again each time; for a CBCT's 10^8 voxels that is seconds of
    // frozen tab before the overlay appears, which is what "the segmentation hangs the
    // page" was. `setCompleteScalarDataArray` is the library's own path for exactly this
    // (`VoxelManager.js:670`): it writes each slice with a single typed-array `set`, marks
    // it modified once, and refreshes the per-image min/max the renderer reads. The length
    // is safe because {@link gridMismatch} has already established the two volumes share a
    // grid, and the narrowing to `Uint8Array` is the same one the per-voxel write did.
    labelmap.voxelManager.setCompleteScalarDataArray(scalarData);

    segmentation.addSegmentations([
        {
            segmentationId,
            representation: {
                type: REPRESENTATIONS.LABELMAP,
                data: { volumeId: segmentationId },
            },
            // **Every label has to be declared here or it cannot be controlled, and
            // cannot even be hidden.** Without a `segments` config,
            // `normalizeSegmentationInput` falls to `normalizedSegments[1] =
            // createDefaultSegment()` -- *one* segment, index 1 -- and everything
            // downstream reads that object: `setSegmentIndexVisibility` returns
            // silently for any index missing from it, and `internalGetHiddenSegmentIndices`,
            // which is what the labelmap actor and the surface renderer actually
            // consult, can never report 2 or 3 as hidden.
            //
            // That was the reported bug on both surfaces: label 1 toggled, labels 2
            // and 3 did nothing, and switching the whole overlay off left everything
            // but label 1 on screen.
            config: { segments: segmentsConfig(labelValues) },
        },
    ]);

    return { ok: true, labelValues, segmentationId };
}

/**
 * The labelmap style a *volume render* needs, which is not the one a slice needs.
 *
 * Cornerstone's labelmap default is a 3px outline over a 50% fill
 * (`displayTools/Labelmap/labelmapConfig.js`), and `labelmapActorStyle` turns that into
 * `setUseLabelOutline(true)` plus a per-segment opacity of `fillAlpha`. On an axial slice
 * that is exactly right: the outline is what makes a tooth's border readable against the
 * bone under it.
 *
 * On the 3D window it is the whole defect. Every label becomes a translucent shell with a
 * bright rim -- coloured *outlines* floating over the study rather than coloured voxels
 * in it, which is what the screenshots show and what "doesn't render coloured voxels"
 * means. There is no border to trace in a volume render; the surface of the label *is*
 * the border, and drawing one over it dilutes both.
 *
 * So the 3D window gets fill and no outline. This is a per-viewport style, not a global
 * one: the same segmentation stays outlined on the three slice windows, where it should
 * be. The inactive variants are set too -- this grid shows one segmentation, so it is
 * "inactive" whenever the user has not selected a segment, and leaving those at their
 * defaults means the overlay changes appearance on a click.
 *
 * **The fill is {@link SEGMENTATION_OPACITY}, not 1.** Removing the outline was only half
 * of it: at full alpha the first label a ray meets terminates it, so the volume render
 * becomes a poster of flat saturated colour with no depth in it -- every tooth a single
 * hue, the mandible a solid slab, and none of the study visible through or behind any of
 * it. That is the second screenshot. A composited labelmap gets its shape from
 * *accumulation*: at 0.5 a ray crossing a crown gathers more colour than one clipping its
 * edge, which is what draws the shading and the darker rim around each structure, and the
 * greyscale study still reads underneath. 0.5 is not a taste: it is the value NiiVue
 * composited this same overlay at (`viewer_grid.js:17`), so this is the picture every
 * stored segmentation was approved against, and the constant declared for it at the top
 * of this module is now the one used.
 *
 * @returns {object} a labelmap style for `segmentation.config.style.setStyle`.
 */
/**
 * The representation config a *volume render* needs, which is not a style.
 *
 * **The blend mode has to be asked for here, and cannot be corrected afterwards.**
 * `createLegacyVolumeLabelmapPlan` reads `config?.blendMode ?? MAXIMUM_INTENSITY_BLEND`
 * and hands that to `addVolumesToViewports`, which passes it to `createVolumeActor`,
 * which sets it on the mapper the moment the actor is built. So Cornerstone's *default*
 * for a labelmap on a volume viewport is a maximum-intensity projection -- and a MIP
 * through a labelmap takes the largest label value along each ray, which is a picture
 * with no depth in it at all: every tooth on the far side of the arch shows through the
 * near side, and rotating changes which colours win rather than what occludes what. That
 * is the reported defect.
 *
 * `viewportManager.setRenderMode` already puts the labelmap actor back onto
 * {@link LABELMAP_RENDER_SPEC}, and it cannot be relied on to do it in time:
 * `addSegmentationRepresentations` is **synchronous and returns undefined** -- it files
 * the representation and leaves the actor to be mounted later by the segmentation render
 * loop -- so the `await` at the call site resolves before any labelmap actor exists, and
 * the re-application walks a viewport holding only the study. Asking for the right blend
 * mode up front removes the race rather than losing it more slowly; the re-application
 * stays, because a drop rebuilds the actors and it is what puts them back.
 *
 * @returns {object} the `config` for a labelmap representation on a 3D viewport.
 */
export function solidVoxelConfig() {
    return { blendMode: BLEND_MODES.COMPOSITE_BLEND };
}

export function solidVoxelStyle() {
    return {
        renderOutline: false,
        renderOutlineInactive: false,
        renderFill: true,
        renderFillInactive: true,
        fillAlpha: SEGMENTATION_OPACITY,
        fillAlphaInactive: SEGMENTATION_OPACITY,
    };
}

/**
 * Show the segmentation in a set of viewports.
 *
 * The same Labelmap representation everywhere, the 3D window included -- see this
 * module's header for why that window does not need, and should not have, a surface.
 *
 * @param {object} options
 * @param {object} options.cornerstone
 * @param {Array<{viewportId: string}>} options.viewports
 * @param {string} [options.segmentationId]
 * @param {number[][]} [options.colorLUT] from {@link paletteFor}.
 * @param {number|null} [options.colorLUTIndex] the index a previous call returned, so a
 *   re-show overwrites that LUT instead of appending another. See below.
 * @param {string[]} [options.solidViewportIds] viewports that render a *volume* rather
 *   than a slice, and therefore want {@link solidVoxelStyle}.
 * @returns {Promise<{shown: string[], colorLUTIndex: number|null}>} the viewports it
 *   reached, and the LUT index to hand back on the next call.
 */
export async function showSegmentation({
    cornerstone,
    viewports,
    segmentationId = SEGMENTATION_ID,
    colorLUT,
    colorLUTIndex = null,
    solidViewportIds = [],
}) {
    const { segmentation } = cornerstone;
    const shown = [];

    // Registered once, not once per viewport *and not once per switch-on*. `addColorLUT`
    // appends and returns a **new** index whenever it is not given one, so adding it
    // inside the loop would leave three identical LUTs behind on a three-window grid --
    // and calling it again on each toggle left one more after every switch-off/on, none of
    // them ever collected. Passing the index back makes the second call overwrite the
    // first (`addColorLUT.js:9`, `indexToUse = index ?? getNextColorLUTIndex()`).
    const lutIndex = colorLUT?.length
        ? segmentation.config.color.addColorLUT(colorLUT, colorLUTIndex ?? undefined)
        : colorLUTIndex;

    for (const { viewportId } of viewports) {
        try {
            await segmentation.addSegmentationRepresentations(viewportId, [
                {
                    segmentationId,
                    type: REPRESENTATIONS.LABELMAP,
                    // A volume render composites; a slice does not care. See
                    // {@link solidVoxelConfig} for why this cannot be set afterwards.
                    ...(solidViewportIds.includes(viewportId)
                        ? { config: solidVoxelConfig() }
                        : {}),
                },
            ]);
        } catch (error) {
            // One viewport failing must not cost the others, and it must not fail
            // silently either -- a 3D window that quietly showed nothing is how this
            // was reported twice.
            console.warn(
                `[ygg-grid] could not show the segmentation in ${viewportId}: ${error.message}`
            );
            continue;
        }
        if (lutIndex !== null) {
            segmentation.config.color.setColorLUT(viewportId, segmentationId, lutIndex);
        }
        // Per viewport, and only for the ones that render a volume: see
        // {@link solidVoxelStyle}. Set after the representation exists, because the
        // style is keyed on the viewport/segmentation pair the representation creates.
        if (solidViewportIds.includes(viewportId)) {
            segmentation.config.style?.setStyle?.(
                { viewportId, segmentationId, type: REPRESENTATIONS.LABELMAP },
                solidVoxelStyle()
            );
        }
        shown.push(viewportId);
    }
    return { shown, colorLUTIndex: lutIndex };
}

/**
 * Hide or show the whole overlay in one viewport.
 *
 * **All of it or none of it.** There was a per-class list here; it was removed on the
 * maintainer's call after it proved more trouble than it was worth to operate. The
 * `segments` map that {@link segmentsConfig} builds is still required and is not part
 * of that UI: Cornerstone hides a labelmap by marking every *segment* hidden, so a
 * segmentation that declares only segment 1 -- which is what happens with no `segments`
 * config -- cannot be switched off past its first class.
 */
export function setOverlayVisible({ cornerstone, viewportId, visible, segmentationId = SEGMENTATION_ID }) {
    cornerstone.segmentation.config.visibility.setSegmentationRepresentationVisibility(
        viewportId,
        { segmentationId },
        Boolean(visible)
    );
}

/**
 * The segmentation's serve URL from `viewer_grid_data.segmentationFile`, or null.
 *
 * The two domains describe it differently and both are served correctly, so this is
 * where the difference is absorbed rather than at the call site:
 *
 *   - **maxillo** emits `{id, fileKey: 'segmentation_nifti', labelMax}` — a member of a
 *     multi-file `cbct_processed` bundle, addressed by a path segment because
 *     `assertLoaderSafeUrl` refuses a query string (F14).
 *   - **brain** emits `{id, file_type}` and its `serve_file` raises `Http404` for any
 *     bundle key at all (`brain/api_views.py:111`), so it must be the plain route.
 *
 * @param {object} options
 * @param {object} options.segmentationFile the payload entry.
 * @param {string} options.namespace
 * @param {string} [options.origin]
 * @param {(o: object) => string} options.volumeUrl the builder from imaging/ids/imageIds.js.
 * @returns {string|null}
 */
export function segmentationUrl({ segmentationFile, namespace, origin, volumeUrl }) {
    const fileId = Number(segmentationFile?.id);
    if (!Number.isInteger(fileId) || fileId <= 0) {
        return null;
    }
    const bundleKey = segmentationFile.fileKey || segmentationFile.file_key || undefined;
    return volumeUrl({
        fileId,
        bundleKey,
        filename: 'segmentation.nii.gz',
        namespace,
        origin,
    });
}
