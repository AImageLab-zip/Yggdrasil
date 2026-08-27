import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';
import { readFile } from 'node:fs/promises';

import {
    FIXED_CBCT_LAYOUT,
    FREE_LAYOUT,
    GRID_WINDOWS,
    ORIENTATIONS,
    ORIENTATION_AXES,
    SLICE_ORIENTATIONS,
    VIEWPORT_TYPES,
    assertEnumsMatch,
    assertWindowIndex,
    isSliceOrientation,
    IMAGE_LOADER_SCHEME,
    VOLUME_ID_SCHEME,
    toolGroupIdFor,
    viewportId,
    viewportSpecFor,
    volumeIdFor,
} from '../imaging/grid/layout.js';
import {
    activeVolumeIds,
    beginLoad,
    clearWindow,
    completeLoad,
    createGridState,
    failLoad,
    isLoadCurrent,
    loadedWindows,
    orientationGroup,
    setFreeScroll,
    setOrientation,
    syncTargets,
    windowAt,
} from '../imaging/grid/windowState.js';
import {
    formatWindow,
    modalityWindowFromVoiRange,
    openingVoi,
    unitFor,
    voiRangeFromModalityWindow,
} from '../imaging/grid/voi.js';
import { residualModalityLut } from '../imaging/metadata/modalityLutModule.js';
import { PRIMARY_TOOLS } from '../imaging/grid/viewportManager.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..', '..');

// ---------------------------------------------------------------------------
// layout.js
// ---------------------------------------------------------------------------

test('the inlined enum strings are the ones the shipped Cornerstone uses', async () => {
    // The point of inlining is that layout.js stays testable without a GPU; the cost is
    // a copy that can drift. Loaded by file URL because @cornerstonejs/core does not
    // publish these subpaths in its `exports` map.
    const enumsDir = join(REPO, 'node_modules', '@cornerstonejs', 'core', 'dist', 'esm', 'enums');
    const { default: ViewportType } = await import(pathToFileURL(join(enumsDir, 'ViewportType.js')));
    const { default: OrientationAxis } = await import(
        pathToFileURL(join(enumsDir, 'OrientationAxis.js'))
    );

    assert.doesNotThrow(() => assertEnumsMatch({ ViewportType, OrientationAxis }));
    assert.equal(VIEWPORT_TYPES.ORTHOGRAPHIC, ViewportType.ORTHOGRAPHIC);
    assert.equal(VIEWPORT_TYPES.VOLUME_3D, ViewportType.VOLUME_3D);
    assert.equal(ORIENTATION_AXES.AXIAL, OrientationAxis.AXIAL);
});

test('assertEnumsMatch names every value that drifted, not just the first', () => {
    assert.throws(
        () =>
            assertEnumsMatch({
                ViewportType: { ORTHOGRAPHIC: 'ortho', VOLUME_3D: 'volume3d' },
                OrientationAxis: { AXIAL: 'axial', SAGITTAL: 'sag', CORONAL: 'coronal' },
            }),
        (error) => {
            assert.match(error.message, /ViewportType\.ORTHOGRAPHIC/);
            assert.match(error.message, /OrientationAxis\.SAGITTAL/);
            return true;
        }
    );
});

test('each slice orientation maps to an orthographic viewport with that axis', () => {
    for (const orientation of SLICE_ORIENTATIONS) {
        assert.deepEqual(viewportSpecFor(orientation), {
            type: VIEWPORT_TYPES.ORTHOGRAPHIC,
            orientation,
        });
        assert.equal(isSliceOrientation(orientation), true);
    }
});

test('the 3D view is a volume3d viewport with NO orientation axis', () => {
    // Passing an orientation to a volume3d viewport is how it silently ends up
    // behaving like an orthographic one.
    assert.deepEqual(viewportSpecFor(ORIENTATIONS.RENDER), {
        type: VIEWPORT_TYPES.VOLUME_3D,
        orientation: null,
    });
    assert.equal(isSliceOrientation(ORIENTATIONS.RENDER), false);
});

test('an unknown orientation is refused rather than defaulted to axial', () => {
    assert.throws(() => viewportSpecFor('oblique'), /Unknown orientation/);
    assert.throws(() => viewportSpecFor(undefined), /Unknown orientation/);
});

test('the fixed CBCT layout is three orthogonal planes plus a volume render', () => {
    assert.equal(FIXED_CBCT_LAYOUT.length, GRID_WINDOWS);
    assert.deepEqual(
        FIXED_CBCT_LAYOUT.map((entry) => entry.orientation),
        ['axial', 'sagittal', 'coronal', 'render']
    );
    // Nothing is lazy. The old adapter hid 3D behind a "Load 3D" button on the grounds
    // that a volume render is expensive, but the volume is already decoded and in GPU
    // memory for the three slice views, so the render costs a transfer function rather
    // than a second load.
    assert.deepEqual(
        FIXED_CBCT_LAYOUT.map((entry) => entry.lazy),
        [false, false, false, false]
    );
    assert.equal(FREE_LAYOUT.length, GRID_WINDOWS);
    assert.ok(FREE_LAYOUT.every((entry) => entry.orientation === ORIENTATIONS.AXIAL && !entry.lazy));
});

test('runtime ids are derived, and window indices are bounds-checked', () => {
    assert.equal(viewportId(0), 'ygg-grid-0');
    assert.equal(viewportId(3), 'ygg-grid-3');
    for (const bad of [-1, 4, 1.5, '0', null, undefined]) {
        assert.throws(() => assertWindowIndex(bad), /Window index must be/);
    }
});

test('two windows on the same file share one volume id, so the volume is cached once', () => {
    // A CBCT is hundreds of megabytes. Keying the id on anything per-window would
    // decode and hold it in GPU memory twice.
    const url = 'https://h/api/processing/files/serve/5/v.nii.gz';
    assert.equal(volumeIdFor(url), volumeIdFor(url));
    assert.equal(volumeIdFor(url), `${VOLUME_ID_SCHEME}:${url}`);
    assert.notEqual(volumeIdFor(url), volumeIdFor(`${url}?x`));
    assert.throws(() => volumeIdFor(''), /needs the loader URL/);
});

test('2D and 3D windows get different tool groups', () => {
    // The 2D tools (length, probe, ROI) have no meaning in a volume render, and the
    // trackball has none in a slice.
    assert.equal(toolGroupIdFor(ORIENTATIONS.AXIAL), toolGroupIdFor(ORIENTATIONS.CORONAL));
    assert.notEqual(toolGroupIdFor(ORIENTATIONS.AXIAL), toolGroupIdFor(ORIENTATIONS.RENDER));
});

// ---------------------------------------------------------------------------
// windowState.js -- load generations
// ---------------------------------------------------------------------------

test('a fresh grid has four empty windows in the layout given', () => {
    const state = createGridState(FIXED_CBCT_LAYOUT);
    assert.equal(state.windows.length, GRID_WINDOWS);
    assert.deepEqual(
        state.windows.map((window) => window.orientation),
        ['axial', 'sagittal', 'coronal', 'render']
    );
    assert.deepEqual(loadedWindows(state), []);
    assert.deepEqual(activeVolumeIds(state), []);
    assert.equal(windowAt(state, 3).lazy, false);
});

test('a superseded load cannot write back over the one that replaced it', () => {
    // Drop volume A on a window, then volume B before A's fetch returns. A must lose,
    // however slow it is -- otherwise the window shows a volume the user replaced.
    const state = createGridState();
    const first = beginLoad(state, 0, { modality: 'cbct', fileId: 1, volumeId: 'nifti:a' });
    const second = beginLoad(state, 0, { modality: 'cbct', fileId: 2, volumeId: 'nifti:b' });

    assert.notEqual(first, second);
    assert.equal(isLoadCurrent(state, 0, first), false);
    assert.equal(isLoadCurrent(state, 0, second), true);

    assert.equal(completeLoad(state, 0, first), false, 'the stale load must not write');
    assert.equal(windowAt(state, 0).fileId, 2);
    assert.equal(windowAt(state, 0).loading, true, 'and must not clear the loading flag');

    assert.equal(completeLoad(state, 0, second), true);
    assert.equal(windowAt(state, 0).loading, false);
});

test('a stale load cannot report its error over a newer load either', () => {
    // The nastier half: the abandoned fetch fails, and without the guard the window
    // shows "load failed" while a perfectly good volume is arriving behind it.
    const state = createGridState();
    const first = beginLoad(state, 0, { volumeId: 'nifti:a' });
    const second = beginLoad(state, 0, { volumeId: 'nifti:b' });

    assert.equal(failLoad(state, 0, first, 'network error'), false);
    assert.equal(windowAt(state, 0).error, null);

    assert.equal(failLoad(state, 0, second, 'network error'), true);
    assert.equal(windowAt(state, 0).error, 'network error');
    assert.equal(windowAt(state, 0).loading, false);
});

test('load generations are per window, so concurrent loads do not cancel each other', () => {
    const state = createGridState();
    const zero = beginLoad(state, 0, { volumeId: 'nifti:a' });
    const one = beginLoad(state, 1, { volumeId: 'nifti:b' });

    assert.equal(isLoadCurrent(state, 0, zero), true);
    assert.equal(isLoadCurrent(state, 1, one), true);
    assert.equal(completeLoad(state, 0, zero), true);
    assert.equal(completeLoad(state, 1, one), true);
});

test('clearing a window also invalidates the load still in flight for it', () => {
    // Clearing a window whose fetch has not returned must not be undone by the fetch
    // returning.
    const state = createGridState();
    const generation = beginLoad(state, 2, { volumeId: 'nifti:a', fileId: 7 });
    clearWindow(state, 2);

    assert.equal(completeLoad(state, 2, generation), false);
    assert.equal(windowAt(state, 2).volumeId, null);
    assert.equal(windowAt(state, 2).fileId, null);
    assert.equal(windowAt(state, 2).loading, false);
});

test('a new load clears the previous orientation warning', () => {
    // F2's warning belongs to the volume, not the window. Carrying it across would
    // label a perfectly well-oriented volume as inferred.
    const state = createGridState();
    const first = beginLoad(state, 0, { volumeId: 'nifti:a' });
    completeLoad(state, 0, first, { orientationWarning: 'declares no orientation' });
    assert.match(windowAt(state, 0).orientationWarning, /no orientation/);

    const second = beginLoad(state, 0, { volumeId: 'nifti:b' });
    assert.equal(windowAt(state, 0).orientationWarning, null);
    completeLoad(state, 0, second);
    assert.equal(windowAt(state, 0).orientationWarning, null);
});

// ---------------------------------------------------------------------------
// windowState.js -- synchronisation policy
// ---------------------------------------------------------------------------

function loadedGrid(layout = FIXED_CBCT_LAYOUT) {
    const state = createGridState(layout);
    for (let index = 0; index < GRID_WINDOWS; index += 1) {
        const generation = beginLoad(state, index, { volumeId: 'nifti:shared', fileId: 1 });
        completeLoad(state, index, generation);
    }
    return state;
}

test('a broadcast reaches every other loaded slice window', () => {
    const state = loadedGrid();
    // Windows 0-2 are slices; window 3 is the 3D render and has no slice to sync.
    assert.deepEqual(syncTargets(state, 0), [1, 2]);
    assert.deepEqual(syncTargets(state, 1), [0, 2]);
});

test('a viewer never receives its own broadcast', () => {
    // Acting on your own event turns a rounding difference between the two directions
    // into an oscillation.
    const state = loadedGrid();
    for (let index = 0; index < GRID_WINDOWS; index += 1) {
        assert.ok(!syncTargets(state, index).includes(index), `window ${index} echoed itself`);
    }
});

test('the 3D render neither broadcasts nor receives', () => {
    const state = loadedGrid();
    assert.deepEqual(syncTargets(state, 3), [], 'a 3D view has no slice to broadcast');
    assert.ok(!syncTargets(state, 0).includes(3), 'and none to receive');
});

test('free scroll opts a window out in BOTH directions', () => {
    // One-directional opt-out reads as a bug the first time a user scrolls a "free"
    // window and watches the others follow.
    const state = loadedGrid();
    setFreeScroll(state, 1, true);

    assert.ok(!syncTargets(state, 0).includes(1), 'a free window must not receive');
    assert.deepEqual(syncTargets(state, 1), [], 'and must not broadcast');

    setFreeScroll(state, 1, false);
    assert.ok(syncTargets(state, 0).includes(1), 'and rejoins when switched back');
});

test('an empty window is not a sync target', () => {
    const state = loadedGrid();
    clearWindow(state, 2);
    assert.deepEqual(syncTargets(state, 0), [1]);
});

test('orientation groups are derived from the windows, so they cannot go stale', () => {
    // The old implementation kept `synchronizationGroups` as a mutable index alongside
    // the window states, which could disagree with them.
    const state = loadedGrid(FREE_LAYOUT); // all four axial
    assert.deepEqual(orientationGroup(state, ORIENTATIONS.AXIAL), [0, 1, 2, 3]);

    setOrientation(state, 2, ORIENTATIONS.CORONAL);
    assert.deepEqual(orientationGroup(state, ORIENTATIONS.AXIAL), [0, 1, 3]);
    assert.deepEqual(orientationGroup(state, ORIENTATIONS.CORONAL), [2]);

    setFreeScroll(state, 0, true);
    assert.deepEqual(orientationGroup(state, ORIENTATIONS.AXIAL), [1, 3]);

    clearWindow(state, 3);
    assert.deepEqual(orientationGroup(state, ORIENTATIONS.AXIAL), [1]);
});

test('setOrientation refuses an orientation the grid cannot render', () => {
    const state = createGridState();
    assert.throws(() => setOrientation(state, 0, 'oblique'), /Unknown orientation/);
});

test('active volume ids are deduplicated, so a shared volume is counted once', () => {
    const state = createGridState(FIXED_CBCT_LAYOUT);
    for (const index of [0, 1, 2]) {
        completeLoad(state, index, beginLoad(state, index, { volumeId: 'nifti:shared' }));
    }
    completeLoad(state, 3, beginLoad(state, 3, { volumeId: 'nifti:other' }));

    assert.deepEqual(activeVolumeIds(state).sort(), ['nifti:other', 'nifti:shared']);
    assert.deepEqual(loadedWindows(state), [0, 1, 2, 3]);

    // And clearing one of three windows on the shared volume must not drop it: this is
    // the answer to "may this volume be evicted?" without a reference count that leaks.
    clearWindow(state, 0);
    assert.ok(activeVolumeIds(state).includes('nifti:shared'));
    clearWindow(state, 1);
    clearWindow(state, 2);
    assert.ok(!activeVolumeIds(state).includes('nifti:shared'));
});

// ---------------------------------------------------------------------------
// voi.js -- windowing in two units at once
// ---------------------------------------------------------------------------

test('a preset survives the round trip through stored units', () => {
    // The property that makes decision #5 workable: a bone window is -450..1050 HU
    // whatever the file's encoding, and comes back as the same numbers.
    for (const header of [
        { scl_slope: 1, scl_inter: -1024 }, // upstream skips: data is raw
        { scl_slope: 2, scl_inter: 0 }, // upstream skips
        { scl_slope: 1, scl_inter: 0 }, // identity
        { scl_slope: 0.5, scl_inter: -100 }, // upstream applies: data is scaled
    ]) {
        const residual = residualModalityLut(header);
        const window = { windowCenter: 300, windowWidth: 1500 };
        const range = voiRangeFromModalityWindow(window, residual);
        const back = modalityWindowFromVoiRange(range, residual);

        assert.ok(Math.abs(back.windowCenter - 300) < 1e-9, JSON.stringify(header));
        assert.ok(Math.abs(back.windowWidth - 1500) < 1e-9, JSON.stringify(header));
        assert.ok(range.lower < range.upper, 'the range must not be inverted');
    }
});

test('the stored range really is shifted for a volume the loader left raw', () => {
    // `(1, -1024)`: scalarData holds raw uint16, so a -450..1050 HU window has to clip
    // at 574..2074 stored. Handing the viewport the HU numbers directly would window
    // on air.
    const residual = residualModalityLut({ scl_slope: 1, scl_inter: -1024 });
    assert.deepEqual(voiRangeFromModalityWindow({ windowCenter: 300, windowWidth: 1500 }, residual), {
        lower: 574,
        upper: 2074,
    });

    // ...and is NOT shifted for one the loader already scaled.
    const applied = residualModalityLut({ scl_slope: 0.5, scl_inter: -100 });
    assert.deepEqual(voiRangeFromModalityWindow({ windowCenter: 300, windowWidth: 1500 }, applied), {
        lower: -450,
        upper: 1050,
    });
});

test('a negative slope does not produce an inverted range', () => {
    // Legal and rare. Without the swap the renderer gets a lower bound above its upper.
    const residual = residualModalityLut({ scl_slope: -1, scl_inter: 100 });
    const range = voiRangeFromModalityWindow({ windowCenter: 0, windowWidth: 200 }, residual);
    assert.ok(range.lower < range.upper);
    const back = modalityWindowFromVoiRange(range, residual);
    assert.ok(Math.abs(back.windowWidth - 200) < 1e-9);
});

test('a CT volume opens on a named preset, in real HU', () => {
    const header = { scl_slope: 1, scl_inter: -1024 };
    const result = openingVoi({ scalarData: new Uint16Array(64), header, modality: 'ct', preset: 'bone' });

    assert.equal(result.source, 'preset');
    assert.equal(result.label, 'Bone');
    assert.deepEqual(result.window, { windowCenter: 300, windowWidth: 1500 });
    assert.deepEqual(result.range, { lower: 574, upper: 2074 });
});

test('a CBCT volume opens on its own robust percentiles, not on a fabricated preset', () => {
    // Decision #16. CBCT greyscale is not calibrated Hounsfield.
    const header = { scl_slope: 1, scl_inter: -1024 };
    const scalarData = Uint16Array.from({ length: 4096 }, (unused, index) =>
        index < 3900 ? 900 + (index % 200) : 2400 + (index % 100)
    );
    const result = openingVoi({ scalarData, header, modality: 'cbct' });

    assert.equal(result.source, 'auto');
    assert.equal(result.label, null);
    assert.ok(result.window.windowWidth > 0);
    assert.ok(result.range.lower < result.range.upper);
});

test('asking for a CBCT preset fails with the reason, not with a silent fallback', () => {
    assert.throws(
        () => openingVoi({ scalarData: new Uint16Array(8), header: {}, modality: 'cbct', preset: 'bone' }),
        /not a calibrated unit/
    );
    assert.throws(
        () => openingVoi({ scalarData: new Uint16Array(8), header: {}, modality: 'ct', preset: 'nope' }),
        /Check the preset name/
    );
});

test('the window readout names its unit, or has none to name', () => {
    // The ambiguity decision #5 removes: the old sliders showed two numbers that meant
    // percent-of-this-volume's-range and read like Hounsfield.
    assert.equal(unitFor('ct'), 'HU');
    assert.equal(unitFor('CT'), 'HU');
    assert.equal(unitFor('cbct'), '', 'CBCT greyscale is not Hounsfield');
    assert.equal(unitFor('mri'), '');

    assert.equal(formatWindow({ windowCenter: 300, windowWidth: 1500 }, 'HU'), 'W 1500 HU / L 300 HU');
    assert.equal(formatWindow({ windowCenter: 812.5, windowWidth: 2799.4 }), 'W 2799 / L 813');
});

// ---------------------------------------------------------------------------
// viewportManager.js <-> the entry
// ---------------------------------------------------------------------------

test('every primary tool is actually registered by the entry', async () => {
    // The invariant: `setPrimaryTool` refuses a name outside PRIMARY_TOOLS, and the
    // tool group can only bind a tool the entry put in GRID_TOOLS. A name in one list
    // and not the other is a toolbar button that throws when clicked.
    //
    // Read as text, not imported: frontend/entries/volume-grid.js imports
    // @cornerstonejs/tools, which needs a DOM. `viewportManager.js` deliberately does
    // not, which is why its half can be imported normally.
    const { PRIMARY_TOOLS } = await import('../imaging/grid/viewportManager.js');
    const entry = await readFile(join(HERE, '..', 'entries', 'volume-grid.js'), 'utf8');

    const block = entry.slice(entry.indexOf('export const GRID_TOOLS'));
    const registered = new Set(
        [...block.slice(0, block.indexOf('};')).matchAll(/^\s{4}(\w+):\s/gm)].map((m) => m[1])
    );

    assert.ok(registered.size > 0, 'GRID_TOOLS could not be parsed out of the entry');
    for (const tool of PRIMARY_TOOLS) {
        assert.ok(registered.has(tool), `PRIMARY_TOOLS names '${tool}', which GRID_TOOLS does not register`);
    }
    // And the permanently-bound ones the tool groups reference by name.
    for (const tool of ['Pan', 'Zoom', 'StackScroll', 'TrackballRotate']) {
        assert.ok(registered.has(tool), `GRID_TOOLS must register '${tool}'`);
    }
});

test('window/level is a primary tool, and pan/zoom/scroll deliberately are not', () => {
    // A user who has picked the length tool still has to navigate, so pan, zoom and
    // scroll keep permanent bindings and are never what the toolbar swaps.
    assert.ok(PRIMARY_TOOLS.includes('WindowLevel'));
    assert.ok(PRIMARY_TOOLS.includes('Length'));
    for (const navigation of ['Pan', 'Zoom', 'StackScroll']) {
        assert.ok(!PRIMARY_TOOLS.includes(navigation), `${navigation} must not be swappable`);
    }
});

// ---------------------------------------------------------------------------
// The volume-id scheme.
//
// The first real harness run errored on all 56 studies with a minified
// "Cannot destructure property 'rows' of 'i' as it is undefined" and nothing pointing
// at the cause. The cause was here: `loadVolumeFromVolumeLoader` picks the volume
// loader by the scheme before the first ':' in the volume id, so a `nifti:`-prefixed
// volume id routed *volume* loading into the *image* loader registered under that same
// scheme. It then looked up `imagePlaneModule` for an id carrying no per-frame
// metadata and destructured undefined.
// ---------------------------------------------------------------------------

test('the volume-id scheme is NOT the image-loader scheme', () => {
    // The whole bug in one assertion.
    assert.notEqual(VOLUME_ID_SCHEME, IMAGE_LOADER_SCHEME);
    assert.equal(IMAGE_LOADER_SCHEME, 'nifti', 'the loader mints nifti:<url>?frame=N ids');
    assert.ok(!volumeIdFor('https://h/v.nii.gz').startsWith(`${IMAGE_LOADER_SCHEME}:`));
});

test('the volume-id scheme falls through to the default streaming volume loader', async () => {
    // `unknownVolumeLoader` is cornerstoneStreamingImageVolumeLoader and core registers
    // no schemes of its own, so any scheme nobody registered gets the streaming loader
    // -- which is exactly what should build a volume out of per-frame imageIds.
    const loaderSrc = await readFile(
        join(REPO, 'node_modules', '@cornerstonejs', 'core', 'dist', 'esm', 'loaders', 'volumeLoader.js'),
        'utf8'
    );
    assert.match(
        loaderSrc,
        /let unknownVolumeLoader = cornerstoneStreamingImageVolumeLoader/,
        'the fallback this design depends on has moved'
    );
    assert.match(
        loaderSrc,
        /const scheme = volumeId\.substring\(0, colonIndex\)/,
        'volume loaders are still selected by the id scheme'
    );
});

test('the entries register the NIfTI loader as an IMAGE loader, not a volume loader', async () => {
    // Read as text: importing an entry needs a DOM. Pinning it anyway, because this is
    // the line whose reversal produced 56 errored studies and an unreadable message.
    for (const name of ['volume-grid.js', 'volume-validation.js']) {
        const entry = await readFile(join(HERE, '..', 'entries', name), 'utf8');
        assert.match(
            entry,
            /imageLoader\.registerImageLoader\(IMAGE_LOADER_SCHEME, cornerstoneNiftiImageLoader\)/,
            `${name} must register the NIfTI loader as an image loader`
        );
        assert.ok(
            !/registerVolumeLoader\(\s*['"]nifti['"]/.test(entry),
            `${name} must not register it as a volume loader`
        );
        assert.ok(
            !/volumeId = `nifti:/.test(entry),
            `${name} must not mint a nifti:-schemed volume id`
        );
    }
});

// ---------------------------------------------------------------------------
// Two cross-file contracts, each of which shipped broken once.
// ---------------------------------------------------------------------------

test('pan and zoom are added to BOTH tool groups, not just the 2D one', async () => {
    // `setToolActive` on a tool that was never *added* to the group does not throw --
    // it logs "Tool Zoom not added to toolGroup, can't set tool mode" and carries on,
    // leaving the 3D view with no pan and no zoom. That is how this shipped.
    const { SHARED_NAVIGATION_TOOLS } = await import('../imaging/grid/viewportManager.js');
    assert.deepEqual(SHARED_NAVIGATION_TOOLS, ['Pan', 'Zoom']);

    const manager = await readFile(join(HERE, '..', 'imaging', 'grid', 'viewportManager.js'), 'utf8');
    // Every shared tool must be bound in the 3D group, and the binding is only legal
    // because the loop above adds it there.
    for (const tool of SHARED_NAVIGATION_TOOLS) {
        assert.match(
            manager,
            new RegExp(`threeD\\.setToolActive\\(tools\\.${tool}\\.toolName`),
            `${tool} is bound in the 3D group`
        );
    }
    assert.match(manager, /SHARED_NAVIGATION_TOOLS\.includes\(name\)/, 'and added to it');

    // And they are registered by the entry in the first place.
    const entry = await readFile(join(HERE, '..', 'entries', 'volume-grid.js'), 'utf8');
    for (const tool of SHARED_NAVIGATION_TOOLS) {
        assert.match(entry, new RegExp(`^\\s{4}${tool}:\\s`, 'm'), `${tool} in GRID_TOOLS`);
    }
});

test('the loaded class is the one the stylesheet hides the drop-hint with', async () => {
    // The template ships a `.drop-hint` placeholder in every window and the stylesheet
    // hides it behind `.viewer-window.loaded`. A mismatch here leaves a grey icon over
    // a black canvas on a window that loaded perfectly -- which is indistinguishable
    // from one that did not.
    const { LOADED_CLASS } = await import('../imaging/grid/viewportManager.js');
    const css = await readFile(join(REPO, 'static', 'css', 'viewer_grid.css'), 'utf8');

    assert.match(
        css,
        new RegExp(`\\.viewer-window\\.${LOADED_CLASS}\\s+\\.drop-hint`),
        `viewer_grid.css must hide .drop-hint under .${LOADED_CLASS}`
    );
});
