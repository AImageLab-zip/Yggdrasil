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
    supportsCrosshairs,
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
import { PRIMARY_TOOLS, setMeasurementToolModes } from '../imaging/grid/viewportManager.js';
import { MEASUREMENT_TOOLS, NAVIGATION_TOOL } from '../imaging/grid/measurements.js';

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
    assert.ok(FREE_LAYOUT.every((entry) => !entry.lazy));
    // Four axial. This surface compares sequences, not planes, and the consequence --
    // that a crosshair has no intersecting planes to draw -- is handled by leaving the
    // tool out rather than by bending the layout around it. See `supportsCrosshairs`.
    assert.deepEqual(
        FREE_LAYOUT.map((entry) => entry.orientation),
        [ORIENTATIONS.AXIAL, ORIENTATIONS.AXIAL, ORIENTATIONS.AXIAL, ORIENTATIONS.AXIAL]
    );
    // And no volume render: unlike the CBCT grid this one has no single primary volume,
    // so a render would have to pick one of four sequences arbitrarily.
    assert.ok(FREE_LAYOUT.every((entry) => entry.orientation !== ORIENTATIONS.RENDER));
});

test('a crosshair is offered only to a layout whose planes intersect', () => {
    // The CBCT grid: three distinct planes among its slice windows.
    assert.equal(supportsCrosshairs(FIXED_CBCT_LAYOUT), true);
    // The brain grid: four windows, one plane. `_calculateToolCenterFromAbsoluteCameras`
    // collapses parallel cameras to a single unique plane and returns null, so the tool
    // centre is never computed and every click on the image is a no-op.
    assert.equal(supportsCrosshairs(FREE_LAYOUT), false);
    // One window is not two, whatever it cuts.
    assert.equal(supportsCrosshairs([{ window: 0, orientation: ORIENTATIONS.AXIAL }]), false);
    // The volume render is not a plane and cannot make up the second one.
    assert.equal(
        supportsCrosshairs([
            { window: 0, orientation: ORIENTATIONS.AXIAL },
            { window: 1, orientation: ORIENTATIONS.RENDER },
        ]),
        false
    );
    // A lazy window is not on screen, so it cannot be intersected with either.
    assert.equal(
        supportsCrosshairs([
            { window: 0, orientation: ORIENTATIONS.AXIAL },
            { window: 1, orientation: ORIENTATIONS.CORONAL, lazy: true },
        ]),
        false
    );
    assert.equal(supportsCrosshairs([]), false);
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
    // Built explicitly rather than from FREE_LAYOUT: what is under test is the
    // derivation, not the brain grid's particular plane assignment.
    const state = loadedGrid(
        Array.from({ length: 4 }, (unused, index) => ({
            window: index,
            orientation: ORIENTATIONS.AXIAL,
            lazy: false,
        }))
    );
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

/** A tool group stub: which tools exist, and what mode each was last put in. */
function fakeToolGroup(present) {
    const modes = {};
    return {
        modes,
        getToolInstance: (name) => (present.includes(name) ? { name } : undefined),
        setToolPassive: (name) => {
            modes[name] = 'Passive';
        },
        setToolDisabled: (name) => {
            modes[name] = 'Disabled';
        },
    };
}

test('switching annotations on gives every measurement tool a mode', () => {
    // The bug this pins: `addTool` writes no `toolOptions` entry, and a tool with no
    // mode is skipped by `getToolsWithModesForElement` -- so the annotation rendering
    // engine never asks it to draw. Measurements restored on page load were therefore
    // invisible however visible their `isVisible` said they were, until the first click
    // on any tool button happened to passive its neighbours. Reported as "the first
    // time I switch annotations on they are not shown".
    const group = fakeToolGroup([...MEASUREMENT_TOOLS]);
    const applied = setMeasurementToolModes({ toolGroup: group, enabled: true });

    assert.deepEqual(applied, [...MEASUREMENT_TOOLS]);
    for (const tool of MEASUREMENT_TOOLS) {
        assert.equal(group.modes[tool], 'Passive', tool);
    }
});

test('switching annotations off disables them, and never the crosshair', () => {
    // Passive on the way in, Disabled on the way out -- and the crosshair is in neither
    // list, because it navigates rather than measures.
    const group = fakeToolGroup([...MEASUREMENT_TOOLS, NAVIGATION_TOOL]);
    setMeasurementToolModes({ toolGroup: group, enabled: false });

    for (const tool of MEASUREMENT_TOOLS) {
        assert.equal(group.modes[tool], 'Disabled', tool);
    }
    assert.equal(group.modes[NAVIGATION_TOOL], undefined);
    assert.ok(!MEASUREMENT_TOOLS.includes(NAVIGATION_TOOL));
});

test('a measurement tool the group never got is skipped, not warned about', () => {
    // The list is shared with the save filter. A name in it that this group does not
    // hold would only warn into the console, which is not where anyone would look.
    const group = fakeToolGroup(['Length']);
    assert.deepEqual(setMeasurementToolModes({ toolGroup: group, enabled: true }), ['Length']);
    assert.doesNotThrow(() => setMeasurementToolModes({ toolGroup: undefined, enabled: true }));
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
    for (const name of ['volume-grid.js']) {
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

test('the orientation marker is pointed at the vendored figure, not GitHub', async () => {
    // Cornerstone's CUSTOM overlay defaults to fetching 3D Slicer's Human.vtp from
    // raw.githubusercontent.com at runtime, and that string is still in the bundle
    // because it is in the vendored library. The override is the only thing stopping
    // the request. The reason it must not be dropped is not that the host is
    // third-party -- CDNs are allowed -- but that raw.githubusercontent.com serves
    // whatever the branch says today, while static/vendor/slicer/ is pinned to a
    // commit. If the override goes, this fails rather than the figure quietly
    // changing under a clinician.
    const entry = await readFile(join(HERE, '..', 'entries', 'volume-grid.js'), 'utf8');
    assert.match(
        entry,
        /orientationMarkerUrl:\s*new URL\('\.\.\/orientation\/Human\.vtp', import\.meta\.url\)/,
        'the entry must resolve the vendored figure from its own bundle'
    );

    const manager = await readFile(join(HERE, '..', 'imaging', 'grid', 'viewportManager.js'), 'utf8');
    assert.match(manager, /polyDataURL: orientationMarkerUrl/, 'and the tool must be told to use it');

    // And the file it names is actually committed.
    const vtp = await readFile(join(REPO, 'static', 'vendor', 'slicer', 'Human.vtp'), 'utf8');
    assert.match(vtp.slice(0, 200), /<VTKFile type="PolyData"/, 'the vendored asset must be real PolyData');
});

test('the build copies the orientation figure into the bundle', async () => {
    // It is resolved through `import.meta.url` relative to the app directory, so it has
    // to land at <build>/orientation/ -- the same mechanism the web workers use, and
    // the same failure mode if it does not: a runtime 404 with no build error.
    const build = await readFile(join(REPO, 'scripts', 'build_frontend.mjs'), 'utf8');
    assert.match(build, /from: 'static\/vendor\/slicer'/);
    assert.match(build, /to: 'orientation'/);
});

test('the crosshair draws no handles and can still be moved', async () => {
    // Two requirements that a previous round proved are in tension. "An additional
    // square and circle on all axis" was reported as clutter; turning
    // `getReferenceLineDraggableRotatable` off removed them and also removed every
    // *translation* the tool performs, so the lines stood still under the cursor.
    const { crosshairLinesOnly, CROSSHAIR_LINE_LENGTH_PX } = await import('../imaging/grid/layout.js');
    const config = crosshairLinesOnly();

    // The handles go through the profile that gates drawing and hit-testing only.
    assert.equal(config.minimal.enabled, true);
    // And the lines stay full length under it -- the profile's own purpose is 40px stubs.
    assert.ok(config.minimal.lineLengthInPx >= 10000);
    assert.equal(config.minimal.lineLengthInPx, CROSSHAIR_LINE_LENGTH_PX);

    // **Neither reference-line callback is overridden.** `_jump` and `_dragCallback` read
    // them raw, so overriding either to false is what disables click-to-navigate.
    assert.equal(config.getReferenceLineDraggableRotatable, undefined);
    assert.equal(config.getReferenceLineSlabThicknessControlsOn, undefined);
    assert.equal(config.getReferenceLineControllable, undefined);

    // The touch profile stays off: it raises the handle radius and draws the handles
    // permanently, which is how the clutter was reported.
    assert.equal(config.mobile.enabled, false);
});

test('the crosshair still separates its drawing switches from its navigation ones', async () => {
    // Read from the vendored tool rather than paraphrased, the way the attenuated-MIP
    // test reads the real shader. `crosshairLinesOnly` rests on exactly one property of
    // this file: `minimal` suppresses the handles in the *render* and *hit-test* paths
    // while `_jump` and `_dragCallback` consult the raw callbacks. A version bump that
    // merges the two would either bring the clutter back or freeze the crosshair again,
    // with no build error either way.
    const tool = await readFile(
        join(REPO, 'node_modules', '@cornerstonejs', 'tools', 'dist', 'esm', 'tools', 'CrosshairsTool.js'),
        'utf8'
    );

    // Drawing: both handle kinds are forced off by the minimal profile.
    assert.match(
        tool,
        /const viewportDraggableRotatable = !minimalCrosshairConfig\.enabled &&/,
        'the rotation handles must still be suppressed by the minimal profile'
    );
    assert.match(
        tool,
        /const viewportSlabThicknessControlsOn = !minimalCrosshairConfig\.enabled &&/,
        'and so must the slab-thickness handles'
    );
    // Hit-testing: neither handle can be grabbed invisibly under it either.
    assert.match(
        tool,
        /_getRotationHandleNearImagePoint\(viewport, annotation, canvasCoords, proximity\) \{\s*const minimalCrosshairConfig = getMinimalCrosshairConfig/,
        'the rotation handle hit test must still consult the minimal profile'
    );
    // Navigation: `_jump` filters on the raw callback, which is why it is left alone.
    assert.match(
        tool,
        /return \(this\._getReferenceLineControllable\(otherViewport\.id\) &&\s*this\._getReferenceLineDraggableRotatable\(otherViewport\.id\) &&\s*sameScene\);/,
        'a click moves the crosshair only through viewports this callback approves'
    );
    // And the minimal branch still clips its line to the canvas, which is what lets a
    // very large lineLengthInPx stand in for a full-width reference line.
    assert.match(
        tool,
        /if \(minimalCrosshairConfig\.enabled\) \{[\s\S]*?liangBarksyClip\(refLinePointThree, refLinePointFour, canvasBox\);/,
        'the minimal branch must still clip to the canvas box'
    );

    // The touch default this configuration overrides: true on any coarse pointer.
    assert.match(tool, /enabled: isMobile\(\)/);
});


test('window/level is shared only by windows showing the same volume', async () => {
    // The CBCT grid is four planes of one study, and a brightness drag in any of them
    // belongs to all of them. The brain grid is four *different* sequences, and
    // Cornerstone's `voiSyncCallback` copies the source's absolute `voiRange` onto every
    // target -- so synchronising there put FLAIR's window on T1, T1c and T2, on load as
    // well as on every drag. Reported as "the brightness of the brains change" and "they
    // seem not to use all 4 the same lighting".
    const { createGridState, voiSyncGroup } = await import('../imaging/grid/windowState.js');
    const { FIXED_CBCT_LAYOUT, FREE_LAYOUT } = await import('../imaging/grid/layout.js');

    const cbct = createGridState(FIXED_CBCT_LAYOUT);
    for (const window of cbct.windows) {
        window.volumeId = 'ygg-volume:one-study';
    }
    assert.deepEqual(voiSyncGroup(cbct), [0, 1, 2, 3]);

    const brain = createGridState(FREE_LAYOUT);
    brain.windows.forEach((window, index) => {
        window.volumeId = `ygg-volume:sequence-${index}`;
    });
    assert.deepEqual(voiSyncGroup(brain), [], 'four different series must not share a window');
});

test('a half-loaded grid synchronises nothing rather than guessing', async () => {
    const { createGridState, voiSyncGroup } = await import('../imaging/grid/windowState.js');
    const { FREE_LAYOUT } = await import('../imaging/grid/layout.js');

    const state = createGridState(FREE_LAYOUT);
    // One window holding a volume has nothing to synchronise with.
    state.windows[0].volumeId = 'ygg-volume:a';
    assert.deepEqual(voiSyncGroup(state), []);

    // Two windows on the same series do share a window/level -- the rule is about the
    // volume, not about which layout the page happens to be using.
    state.windows[2].volumeId = 'ygg-volume:a';
    assert.deepEqual(voiSyncGroup(state), [0, 2]);

    // And one disagreeing window is enough to stop it: pushing an absolute range from
    // one series onto another is what misreports an intensity.
    state.windows[3].volumeId = 'ygg-volume:b';
    assert.deepEqual(voiSyncGroup(state), []);
});

test('the VOI sync callback still copies an absolute range, which is why it is scoped', async () => {
    // Pinned against the vendored library. If a bump made this relative -- window width
    // and centre as a fraction of each volume's own range -- the scoping above would be
    // unnecessary rather than wrong, and this is where to notice.
    const callback = await readFile(
        join(REPO, 'node_modules', '@cornerstonejs', 'tools', 'dist', 'esm',
             'synchronizers', 'callbacks', 'voiSyncCallback.js'),
        'utf8'
    );
    assert.match(callback, /const tProperties = \{\s*voiRange: range,/);
});

test('the grid reuses a rendering engine the panoramic already opened under its id', async () => {
    // `entries/panoramic-cpr.js` attaches its two viewports to the grid's engine, by the
    // grid's id, to stay inside the WebGL context budget. `new RenderingEngine(id)` ends
    // with `renderingEngineCache.set(this)`, which overwrites that id silently -- and
    // `getEnabledElement` resolves every viewport through that cache, so the displaced
    // engine's viewports keep drawing while the library can no longer find them: no
    // annotation is rendered or hit-tested on them, and no segmentation representation on
    // them can be shown or hidden.
    const { readFileSync } = await import('node:fs');
    const core = readFileSync(
        'node_modules/@cornerstonejs/core/dist/esm/RenderingEngine/BaseRenderingEngine.js',
        'utf8'
    );
    assert.match(
        core,
        /this\.id = id \? id : uuidv4\(\);[\s\S]{0,200}renderingEngineCache\.set\(this\);/,
        'the constructor still files itself under its id, replacing whatever was there'
    );
    const resolve = readFileSync(
        'node_modules/@cornerstonejs/core/dist/esm/getEnabledElement.js',
        'utf8'
    );
    assert.match(
        resolve,
        /const renderingEngine = getRenderingEngine\(renderingEngineId\);/,
        'and everything resolves a viewport through that cache'
    );

    const manager = readFileSync('frontend/imaging/grid/viewportManager.js', 'utf8');
    assert.match(
        manager,
        /getRenderingEngine\?\.\(RENDERING_ENGINE_ID\) \?\? new RenderingEngine\(RENDERING_ENGINE_ID\)/,
        'so the grid must reuse rather than construct unconditionally'
    );
    const panoramic = readFileSync('frontend/entries/panoramic-cpr.js', 'utf8');
    assert.match(
        panoramic,
        /getRenderingEngine\(GRID_ENGINE_ID\) \?\? new RenderingEngine\(GRID_ENGINE_ID\)/,
        'and so must the panoramic, from its side'
    );
});
