import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
    BRAIN_COLOURS,
    goldenHue,
    gridMismatch,
    labelValuesIn,
    loadSegmentation,
    paletteFor,
    segmentationUrl,
    segmentsConfig,
    showSegmentation,
    REPRESENTATIONS,
} from '../imaging/grid/segmentation.js';
import { SEGMENTATION_IDS } from '../imaging/grid/bootstrap.js';
import { volumeUrl } from '../imaging/ids/imageIds.js';

// --- the palette, which must not change under existing work -----------------------

test('three or fewer labels get the green/red/blue the help modal documents', () => {
    const lut = paletteFor([1, 2, 3]);
    assert.deepEqual(lut[0], [0, 0, 0, 0], 'background is transparent');
    assert.deepEqual(lut[1], [...BRAIN_COLOURS[0], 255]);
    assert.deepEqual(lut[2], [...BRAIN_COLOURS[1], 255]);
    assert.deepEqual(lut[3], [...BRAIN_COLOURS[2], 255]);
});

test('more than three labels walk the golden ratio, as NiiVue did', () => {
    // The values pinned here are what `niivue_viewer.js:163-183` produced. Recover it
    // with `git show c03afa6^:static/js/modality_viewers/niivue_viewer.js`. A change
    // here recolours every segmentation anyone has ever approved.
    const lut = paletteFor([1, 2, 3, 11, 12]);
    assert.deepEqual(lut[1].slice(0, 3), goldenHue(1));
    assert.deepEqual(lut[11].slice(0, 3), goldenHue(11));
    assert.equal(lut[1][3], 255);
});

test('a label that is absent gets a transparent entry, not the next colour along', () => {
    // Cornerstone indexes the LUT by segment index, so the array has to stay dense:
    // a sparse map would paint segment 12 with segment 3's colour.
    const lut = paletteFor([1, 12]);
    assert.equal(lut.length, 13);
    assert.deepEqual(lut[5], [0, 0, 0, 0]);
    assert.equal(lut[12][3], 255);
});

test('an empty label set yields only the transparent background entry', () => {
    assert.deepEqual(paletteFor([]), [[0, 0, 0, 0]]);
    assert.deepEqual(paletteFor([0, -1, 1.5]), [[0, 0, 0, 0]]);
});

// --- reading the labelmap ---------------------------------------------------------

test('only the non-zero values are reported, sorted', () => {
    assert.deepEqual(labelValuesIn(new Uint8Array([0, 3, 0, 1, 3, 0, 2])), [1, 2, 3]);
    assert.deepEqual(labelValuesIn(new Uint8Array([0, 0, 0])), []);
});

// --- the refusal, which is the whole point ---------------------------------------

test('a segmentation on a different grid is refused rather than resampled', () => {
    const reference = { dimensions: [256, 256, 180], spacing: [1, 1, 1] };
    const segmentation = { dimensions: [128, 128, 90], spacing: [2, 2, 2] };
    const reason = gridMismatch(reference, segmentation);
    assert.match(reason, /not the same study/);
    assert.match(reason, /128×128×90/);
});

test('matching dimensions but different voxel size is also a mismatch', () => {
    // The failure this catches drifts with distance rather than being uniformly
    // offset, which reads as a segmentation that slides rather than one that is wrong.
    const reason = gridMismatch(
        { dimensions: [256, 256, 180], spacing: [1, 1, 1] },
        { dimensions: [256, 256, 180], spacing: [1, 1, 1.5] }
    );
    assert.match(reason, /voxel size/);
});

test('the same grid is not a mismatch', () => {
    const grid = { dimensions: [256, 256, 180], spacing: [0.5, 0.5, 0.5] };
    assert.equal(gridMismatch(grid, { ...grid }), null);
});

// --- the URL, where the two domains differ ----------------------------------------

test('a CBCT segmentation is addressed by its bundle key, in the path', () => {
    // A query string would break the loader: it appends `?frame=N` unconditionally,
    // so `?file_key=x?frame=0` parses frame as part of the key. See imageIds.js F14.
    const url = segmentationUrl({
        segmentationFile: { id: 42, fileKey: 'segmentation_nifti', labelMax: 98 },
        namespace: 'maxillo',
        origin: 'https://ygg.example',
        volumeUrl,
    });
    assert.match(url, /\/maxillo\/api\/processing\/files\/serve\/42\/key\/segmentation_nifti\//);
    assert.ok(!url.includes('?'), 'no query string reaches the NIfTI loader');
});

test('a brain segmentation uses the plain route, because brain refuses bundle keys', () => {
    // brain/api_views.py:111 raises Http404 for any bundle key at all.
    const url = segmentationUrl({
        segmentationFile: { id: 7, file_type: 'braintumor_mri_seg_processed' },
        namespace: 'brain',
        origin: 'https://ygg.example',
        volumeUrl,
    });
    assert.match(url, /\/brain\/api\/processing\/files\/serve\/7\/segmentation\.nii\.gz$/);
    assert.ok(!url.includes('/key/'));
});

test('a patient with no segmentation yields no URL rather than a broken one', () => {
    for (const segmentationFile of [null, undefined, {}, { id: 0 }]) {
        assert.equal(
            segmentationUrl({ segmentationFile, namespace: 'brain', volumeUrl }),
            null
        );
    }
});

// --- loading, with Cornerstone stubbed --------------------------------------------

function fakeCornerstone({ segDims = [4, 1, 1], segSpacing = [1, 1, 1], data } = {}) {
    const scalar = data ?? new Uint8Array([0, 1, 2, 0]);
    const written = [];
    const volumes = {
        ref: { dimensions: [4, 1, 1], spacing: [1, 1, 1] },
    };
    const added = [];
    return {
        added,
        written,
        bag: {
            cache: { getVolume: (id) => volumes[id] ?? null },
            createNiftiImageIdsAndCacheMetadata: async () => ['nifti:x?frame=0'],
            volumeLoader: {
                createAndCacheVolume: async (id) => {
                    volumes[id] = {
                        dimensions: segDims,
                        spacing: segSpacing,
                        voxelManager: { getCompleteScalarDataArray: () => scalar },
                        imageData: {},
                        // `awaitVolumeLoad` resolves off this rather than waiting for a
                        // per-frame callback that a stub never fires; see volumeLoading.js.
                        imageIds: ['nifti:x?frame=0'],
                        loadStatus: { loaded: true },
                        load: () => {},
                    };
                    return volumes[id];
                },
                // The derived labelmap is an `ImageVolume`, so its voxel manager is the
                // per-slice one built by `VoxelManager.createImageVolumeVoxelManager`;
                // `setCompleteScalarDataArray` is the write it offers (VoxelManager.js:670)
                // and the only one this module uses.
                createAndCacheDerivedLabelmapVolume: () => ({
                    voxelManager: {
                        setCompleteScalarDataArray: (values) => written.push(...values),
                    },
                }),
            },
            segmentation: {
                addSegmentations: (entries) => added.push(...entries),
                addSegmentationRepresentations: async () => {},
                config: {
                    color: { addColorLUT: () => 3, setColorLUT: () => {} },
                    visibility: {},
                },
            },
        },
    };
}

test('a mismatched segmentation returns a reason instead of registering anything', async () => {
    const stub = fakeCornerstone({ segDims: [8, 1, 1] });
    const result = await loadSegmentation({
        cornerstone: stub.bag,
        referenceVolumeId: 'ref',
        url: 'https://ygg.example/seg.nii.gz',
    });
    assert.equal(result.ok, false);
    assert.match(result.reason, /not the same study/);
    assert.equal(stub.added.length, 0, 'nothing is registered for a grid we refused');
});

test('an all-background segmentation says so rather than showing an empty overlay', async () => {
    const stub = fakeCornerstone({ data: new Uint8Array([0, 0, 0, 0]) });
    const result = await loadSegmentation({
        cornerstone: stub.bag,
        referenceVolumeId: 'ref',
        url: 'https://ygg.example/seg.nii.gz',
    });
    assert.equal(result.ok, false);
    assert.match(result.reason, /empty/);
});

test('a matching segmentation is written into the labelmap in one pass and registered', async () => {
    const stub = fakeCornerstone();
    const result = await loadSegmentation({
        cornerstone: stub.bag,
        referenceVolumeId: 'ref',
        url: 'https://ygg.example/seg.nii.gz',
    });
    assert.equal(result.ok, true);
    assert.deepEqual(result.labelValues, [1, 2]);
    // One write of the whole array, not one call per voxel: the per-voxel loop this
    // replaced re-resolved the owning slice on every call and re-marked it dirty, which
    // on a CBCT's hundred million voxels was seconds of frozen page before the overlay
    // appeared.
    assert.deepEqual(stub.written, [0, 1, 2, 0]);
    assert.equal(stub.added[0].representation.type, REPRESENTATIONS.LABELMAP);
});

test('a volume that is not loaded is refused with a sentence, not a TypeError', async () => {
    const stub = fakeCornerstone();
    const result = await loadSegmentation({
        cornerstone: stub.bag,
        referenceVolumeId: 'nothing-here',
        url: 'https://ygg.example/seg.nii.gz',
    });
    assert.equal(result.ok, false);
    assert.match(result.reason, /no volume/);
});

// --- the representation per viewport, which is the 3D fix -------------------------

test('every window gets a labelmap, the 3D one included', async () => {
    // The first version gave the 3D window a Surface, believing a volume3d viewport
    // could not render a labelmap. It can: `getViewportLabelmapRenderMode` returns
    // 'volume' for anything extending `BaseVolumeViewport`, and `VolumeViewport3D`
    // does. The surface route needed the polySeg add-on and extracted one mesh per
    // label, each copying the whole volume -- thirty-odd times for a CBCT.
    const asked = [];
    const cornerstone = {
        segmentation: {
            addSegmentationRepresentations: async (viewportId, entries) => {
                asked.push([viewportId, entries[0].type]);
            },
            config: { color: { addColorLUT: () => 1, setColorLUT: () => {} } },
        },
    };
    await showSegmentation({
        cornerstone,
        viewports: [{ viewportId: 'ygg-grid-0' }, { viewportId: 'ygg-grid-3' }],
        colorLUT: paletteFor([1]),
    });
    assert.deepEqual(asked, [
        ['ygg-grid-0', REPRESENTATIONS.LABELMAP],
        ['ygg-grid-3', REPRESENTATIONS.LABELMAP],
    ]);
    assert.equal(REPRESENTATIONS.SURFACE, undefined, 'no surface path remains');
});

test('one viewport failing does not cost the others', async () => {
    // The 3D one is the likeliest to fail: it is the only one doing a wasm conversion.
    const cornerstone = {
        segmentation: {
            addSegmentationRepresentations: async (viewportId) => {
                if (viewportId === 'ygg-grid-3') {
                    throw new Error('polyseg said no');
                }
            },
            config: { color: { addColorLUT: () => 1, setColorLUT: () => {} } },
        },
    };
    const { shown } = await showSegmentation({
        cornerstone,
        viewports: [{ viewportId: 'ygg-grid-0' }, { viewportId: 'ygg-grid-3' }],
    });
    assert.deepEqual(shown, ['ygg-grid-0']);
});

// --- the template interface -------------------------------------------------------

test('the shared toolbar carries the ids the control looks up', () => {
    // Phase 5's lesson, restated: a template id joining two files is an untested
    // interface, and a rename leaves the JS holding null and a dead button.
    const html = readFileSync(
        new URL('../../templates/common/sections/volume_grid_toolbar.html', import.meta.url),
        'utf8'
    );
    assert.ok(html.includes(`id="${SEGMENTATION_IDS.toggle}"`), 'toggle');
    // And no per-class list: the overlay is all of it or none of it.
    assert.ok(!html.includes('viewerSegClasses'), 'no class list');
    // A switch, not a pressed-looking button: `isOn` reads `aria-checked`, and the
    // control renders the word beside it into `[data-mode-state]`.
    assert.match(html, /id="viewerSegToggle"[^>]*role="switch"/s);
    assert.ok(html.includes('data-mode-state'));
});

test('both grid pages include that toolbar', () => {
    // The brain page had none of it -- no crosshair, no measurement tools, no save --
    // although the grid builds both tool groups for every surface.
    for (const path of [
        'templates/brain/patient_detail_content.html',
        'templates/maxillo/patient_detail_content.html',
    ]) {
        const html = readFileSync(new URL(`../../${path}`, import.meta.url), 'utf8');
        assert.ok(
            html.includes("common/sections/volume_grid_toolbar.html"),
            `${path}: includes the shared toolbar`
        );
    }
});

test('the colour LUT is registered once, not once per viewport', async () => {
    // addColorLUT appends and returns a new index every call, so doing it in the loop
    // leaves a duplicate LUT behind per window and another on every reapply.
    let added = 0;
    const cornerstone = {
        segmentation: {
            addSegmentationRepresentations: async () => {},
            config: {
                color: {
                    addColorLUT: () => {
                        added += 1;
                        return added;
                    },
                    setColorLUT: () => {},
                },
            },
        },
    };
    await showSegmentation({
        cornerstone,
        viewports: [
            { viewportId: 'a' },
            { viewportId: 'b' },
            { viewportId: 'c' },
        ],
        colorLUT: paletteFor([1, 2]),
    });
    assert.equal(added, 1);
});

test('the overlay names the volumes it owns so they survive an eviction sweep', async () => {
    // `releaseUnusedVolumes` evicts everything no window holds. The labelmap and its
    // source are in use without being on screen, and dropping them mid-session leaves
    // Cornerstone's segmentation state pointing at volumes the cache no longer has.
    const { ownedVolumeIds, SEGMENTATION_ID } = await import('../imaging/grid/segmentation.js');
    assert.deepEqual(ownedVolumeIds(), [SEGMENTATION_ID, `${SEGMENTATION_ID}-source`]);
});

// --- the reported bug: only label 1 could be toggled -------------------------------

test('every present label is declared as a segment, not just the first', () => {
    // Still required with the per-class UI gone: Cornerstone hides a labelmap by
    // marking every *segment* hidden, so a segmentation declaring only segment 1
    // cannot be switched off past its first class.
    // The bug, exactly. With no `segments` config, Cornerstone's
    // `normalizeSegmentationInput` falls to `normalizedSegments[1] =
    // createDefaultSegment()` -- one segment -- and both
    // `setSegmentIndexVisibility` and `internalGetHiddenSegmentIndices` read that
    // object. So labels 2 and 3 could not be hidden individually, and could not be
    // hidden by switching the whole overlay off either.
    const config = segmentsConfig([1, 2, 3]);
    assert.deepEqual(Object.keys(config), ['1', '2', '3']);
    assert.equal(config[2].label, 'Label 2');
    // Exactly one active segment, and it is the lowest.
    assert.equal(config[1].active, true);
    assert.equal(config[3].active, false);
});

test('a sparse CBCT label set keeps its own values as segment indices', () => {
    // The labelmap stores the label in the voxel and `labelToSegmentIndex` defaults to
    // the identity, so segment 21 is label 21 and the colour LUT is indexed the same.
    const config = segmentsConfig([11, 21, 48]);
    assert.deepEqual(Object.keys(config), ['11', '21', '48']);
});

test('the segmentation is registered with all of its labels declared', async () => {
    const stub = fakeCornerstone({ data: new Uint8Array([0, 1, 2, 3]) });
    const result = await loadSegmentation({
        cornerstone: stub.bag,
        referenceVolumeId: 'ref',
        url: 'https://ygg.example/seg.nii.gz',
    });
    assert.equal(result.ok, true);
    assert.deepEqual(Object.keys(stub.added[0].config.segments), ['1', '2', '3']);
});

// --- the class list's shape --------------------------------------------------------

test('only the labels the file contains are offered', () => {
    // A CBCT declares `labelMax: 98`. What it carries is what the voxels say.
    assert.deepEqual(labelValuesIn(new Uint8Array([0, 3, 0, 3])), [3]);
    assert.deepEqual(labelValuesIn(new Uint8Array([0, 48, 11, 0, 48])), [11, 48]);
});

// ---------------------------------------------------------------------------
// The 3D window
// ---------------------------------------------------------------------------

test('the volume render gets solid voxels, and the slices keep their outline', async () => {
    // Cornerstone's labelmap default is a 3px outline over a 50% fill
    // (`displayTools/Labelmap/labelmapConfig.js`), which is right on a slice and is the
    // whole defect on a volume render: every label became a translucent shell with a
    // bright rim -- coloured outlines floating over the study rather than coloured
    // voxels in it.
    const { SEGMENTATION_OPACITY, showSegmentation, solidVoxelStyle } = await import(
        '../imaging/grid/segmentation.js'
    );

    const styled = [];
    const cornerstone = {
        segmentation: {
            addSegmentationRepresentations: async () => {},
            config: {
                color: { addColorLUT: () => 7, setColorLUT: () => {} },
                style: { setStyle: (specifier, style) => styled.push([specifier.viewportId, style]) },
            },
        },
    };

    const { shown: reached } = await showSegmentation({
        cornerstone,
        viewports: [
            { viewportId: 'ygg-grid-0' },
            { viewportId: 'ygg-grid-3' },
        ],
        colorLUT: [[0, 0, 0, 0], [1, 2, 3, 255]],
        solidViewportIds: ['ygg-grid-3'],
    });

    assert.deepEqual(reached, ['ygg-grid-0', 'ygg-grid-3']);
    assert.equal(styled.length, 1, 'only the volume window is restyled');
    assert.equal(styled[0][0], 'ygg-grid-3');
    assert.deepEqual(styled[0][1], solidVoxelStyle());
    assert.equal(solidVoxelStyle().renderOutline, false);
    // Fill, but not *opaque* fill. At alpha 1 the first label a ray meets terminates it
    // and the volume render is flat saturated colour with no depth and no study behind
    // it; the accumulation at 0.5 is what shades a crown and darkens its rim. 0.5 is the
    // value NiiVue composited this same overlay at, so it is also the picture the archive
    // was approved against.
    assert.equal(solidVoxelStyle().fillAlpha, SEGMENTATION_OPACITY);
    assert.equal(SEGMENTATION_OPACITY, 0.5);
    // The inactive variants matter: this grid shows one segmentation, so it is
    // "inactive" whenever no segment is selected, and leaving those at their defaults
    // makes the overlay change appearance on a click.
    assert.equal(solidVoxelStyle().renderOutlineInactive, false);
    assert.equal(solidVoxelStyle().fillAlphaInactive, SEGMENTATION_OPACITY);
});

test('a labelmap is composited, never maximum-intensity projected', async () => {
    // A MIP through a labelmap takes the largest *label value* along the ray, so the
    // highest-numbered tooth wins wherever two overlap on screen regardless of which is
    // in front. Applying the study's mode to every actor -- itself a fix for a real
    // mismatch -- is what put the labelmap on that projection.
    const { BLEND_MODES, LABELMAP_RENDER_SPEC, applyLabelmapRenderMode, renderModeSpec } =
        await import('../imaging/grid/renderModes.js');

    assert.equal(LABELMAP_RENDER_SPEC.blendMode, BLEND_MODES.COMPOSITE_BLEND);
    assert.notEqual(LABELMAP_RENDER_SPEC.blendMode, renderModeSpec('amip').blendMode);
    assert.deepEqual(LABELMAP_RENDER_SPEC.shaderReplacements, []);
    // Shaded: a composite of one flat colour is a silhouette, and a segmentation whose
    // crowns and roots cannot be told apart is not showing anatomy.
    assert.equal(LABELMAP_RENDER_SPEC.shade, true);

    const applied = { blend: null, shade: null, replacements: null };
    const actor = {
        getMapper: () => ({
            getViewSpecificProperties: () => ({}),
            setViewSpecificProperties: (value) => {
                applied.replacements = value.OpenGL.ShaderReplacements;
            },
            setBlendMode: (value) => {
                applied.blend = value;
            },
        }),
        getProperty: () => ({
            setShade: (value) => {
                applied.shade = value;
            },
            setAmbient: () => {},
            setDiffuse: () => {},
            setSpecular: () => {},
        }),
    };
    applyLabelmapRenderMode(actor);
    assert.equal(applied.blend, BLEND_MODES.COMPOSITE_BLEND);
    assert.equal(applied.shade, true);
    assert.deepEqual(applied.replacements, []);
});

test('the 3D window asks for a composited labelmap, and asks at registration time', async (t) => {
    // **Why this cannot be a later correction.** Cornerstone's own default for a labelmap
    // on a volume viewport is `MAXIMUM_INTENSITY_BLEND` (`legacyVolumePlan.js`:
    // `config?.blendMode ?? MAXIMUM_INTENSITY_BLEND`), and `createVolumeActor` sets it on
    // the mapper the moment the actor is built. A MIP through a labelmap takes the largest
    // *label value* along each ray, which is a picture with no depth in it: every tooth on
    // the far side of the arch shows through the near side and rotating changes which
    // colours win rather than what occludes what.
    //
    // `viewportManager.setRenderMode` does put the actor back on a composite -- and it
    // cannot be relied on to be in time, which is the second half of this test.
    const { showSegmentation, solidVoxelConfig } = await import('../imaging/grid/segmentation.js');
    const { BLEND_MODES } = await import('../imaging/grid/renderModes.js');

    const requested = new Map();
    const cornerstone = {
        segmentation: {
            addSegmentationRepresentations: async (viewportId, entries) => {
                requested.set(viewportId, entries[0]);
            },
            config: {
                color: { addColorLUT: () => 7, setColorLUT: () => {} },
                style: { setStyle: () => {} },
            },
        },
    };

    await showSegmentation({
        cornerstone,
        viewports: [{ viewportId: 'ygg-grid-0' }, { viewportId: 'ygg-grid-3' }],
        colorLUT: [[0, 0, 0, 0], [1, 2, 3, 255]],
        solidViewportIds: ['ygg-grid-3'],
    });

    assert.equal(requested.get('ygg-grid-3').config.blendMode, BLEND_MODES.COMPOSITE_BLEND);
    assert.equal(solidVoxelConfig().blendMode, BLEND_MODES.COMPOSITE_BLEND);
    // A slice does not composite a volume and must not be told to.
    assert.equal(requested.get('ygg-grid-0').config, undefined);
});

test('addSegmentationRepresentations returns before any actor exists', async () => {
    // The fact the fix above rests on. Upstream's `addSegmentationRepresentations` is
    // **synchronous and returns undefined** -- it files the representation and leaves the
    // actor to the segmentation render loop -- so awaiting it and then walking the
    // viewport's actors finds only the study, and the re-application of the render mode
    // that was meant to correct the blend mode corrects nothing.
    const { segmentation } = await import('@cornerstonejs/tools');
    assert.equal(
        segmentation.addSegmentationRepresentations('no-such-viewport', []),
        undefined,
        'no promise to await: the mount happens later'
    );
});

test('switching the overlay off and on does not mint a second colour LUT', async () => {
    // `addColorLUT` appends and returns a *new* index whenever it is not given one, so
    // every switch-on left one more identical LUT behind, none of them ever collected.
    // Handing the index back is what makes the second call overwrite the first.
    const { showSegmentation } = await import('../imaging/grid/segmentation.js');
    const added = [];
    const applied = [];
    const cornerstone = {
        segmentation: {
            addSegmentationRepresentations: async () => {},
            config: {
                color: {
                    addColorLUT: (lut, index) => {
                        const used = index ?? added.length + 7;
                        added.push(used);
                        return used;
                    },
                    setColorLUT: (viewportId, segmentationId, index) => applied.push(index),
                },
                style: { setStyle: () => {} },
            },
        },
    };
    const viewports = [{ viewportId: 'ygg-grid-0' }];
    const colorLUT = [[0, 0, 0, 0], [1, 2, 3, 255]];

    const first = await showSegmentation({ cornerstone, viewports, colorLUT });
    const second = await showSegmentation({
        cornerstone,
        viewports,
        colorLUT,
        colorLUTIndex: first.colorLUTIndex,
    });

    assert.equal(second.colorLUTIndex, first.colorLUTIndex);
    assert.deepEqual(added, [first.colorLUTIndex, first.colorLUTIndex]);
    assert.deepEqual(applied, [first.colorLUTIndex, first.colorLUTIndex]);
});
