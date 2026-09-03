import test from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';

import {
    DISPLAY_LETTERS,
    OVERLAY_CLASSES,
    PANEL_LABELS,
    createOverlay,
    edgeLabels,
    sliceText,
    toDisplayLetters,
    updateOverlay,
    viewRightFrom,
} from '../imaging/grid/viewportOverlay.js';

const REPO = '/srv/Yggdrasil';
const ORIENTATION_DIR = join(
    REPO, 'node_modules', '@cornerstonejs', 'tools', 'dist', 'esm', 'utilities', 'orientation'
);

/**
 * Cornerstone's real utilities, loaded from node_modules. What these tests verify is
 * Cornerstone's behaviour, not a paraphrase of it -- which is the point of injecting
 * them rather than reimplementing the anatomy.
 */
const { default: getOrientationStringLPS } = await import(
    pathToFileURL(join(ORIENTATION_DIR, 'getOrientationStringLPS.js'))
);
const { default: invertOrientationStringLPS } = await import(
    pathToFileURL(join(ORIENTATION_DIR, 'invertOrientationStringLPS.js'))
);
const UTILITIES = { getOrientationStringLPS, invertOrientationStringLPS };

/** Cornerstone's own MPR camera presets (`constants/mprCameraValues.js`). */
const PRESETS = {
    axial: { viewPlaneNormal: [0, 0, -1], viewUp: [0, -1, 0], viewRight: [1, 0, 0] },
    sagittal: { viewPlaneNormal: [1, 0, 0], viewUp: [0, 0, 1], viewRight: [0, 1, 0] },
    coronal: { viewPlaneNormal: [0, -1, 0], viewUp: [0, 0, 1], viewRight: [1, 0, 0] },
};

test('viewRightFrom reproduces the viewRight Cornerstone states for every plane', async () => {
    // Derived rather than read off the constant, because getCamera() does not always
    // carry viewRight and a rotated viewport has one no constant knows. Checked against
    // the constant so the derivation is not merely plausible.
    const { default: mprCameraValues } = await import(
        pathToFileURL(join(REPO, 'node_modules', '@cornerstonejs', 'core', 'dist', 'esm', 'constants', 'mprCameraValues.js'))
    );
    for (const [plane, preset] of Object.entries(PRESETS)) {
        assert.deepEqual(viewRightFrom(preset), preset.viewRight, plane);
        assert.deepEqual(
            viewRightFrom(mprCameraValues[plane]),
            Array.from(mprCameraValues[plane].viewRight),
            `${plane} against Cornerstone's own constant`
        );
    }
});

test('the edge letters match the reference viewer for all three planes', () => {
    assert.deepEqual(edgeLabels(PRESETS.axial, UTILITIES), {
        left: 'R', right: 'L', top: 'A', bottom: 'P',
    });
    assert.deepEqual(edgeLabels(PRESETS.sagittal, UTILITIES), {
        left: 'A', right: 'P', top: 'S', bottom: 'I',
    });
    assert.deepEqual(edgeLabels(PRESETS.coronal, UTILITIES), {
        left: 'R', right: 'L', top: 'S', bottom: 'I',
    });
});

test('head and foot are shown as superior and inferior', () => {
    // Cornerstone says H/F; radiology says S/I. The only letter decision that is ours.
    assert.deepEqual(DISPLAY_LETTERS, { H: 'S', F: 'I' });
    assert.equal(toDisplayLetters('H'), 'S');
    assert.equal(toDisplayLetters('F'), 'I');
    assert.equal(toDisplayLetters('RH'), 'RS', 'oblique labels are multi-letter');
    assert.equal(toDisplayLetters('L'), 'L', 'the in-plane letters are unchanged');
});

test('a rotated camera relabels itself, which a per-plane table could not', () => {
    // Flip the axial view upside down: top and bottom must swap.
    const flipped = { viewPlaneNormal: [0, 0, -1], viewUp: [0, 1, 0] };
    const labels = edgeLabels(flipped, UTILITIES);
    assert.equal(labels.top, 'P');
    assert.equal(labels.bottom, 'A');
});

test('a camera that is not ready yields blank labels rather than throwing', () => {
    assert.deepEqual(edgeLabels(undefined, UTILITIES), { top: '', bottom: '', left: '', right: '' });
    assert.deepEqual(edgeLabels({}, UTILITIES), { top: '', bottom: '', left: '', right: '' });
});

test('the slice counter is one-based, as every viewer shows it', () => {
    assert.equal(sliceText(0, 251), '1 / 251');
    assert.equal(sliceText(114, 251), '115 / 251');
    assert.equal(sliceText(250, 251), '251 / 251');
});

test('the slice counter is blank when there is nothing to count', () => {
    // "1 / 0" reads as a loaded volume with no slices.
    assert.equal(sliceText(0, 0), '');
    assert.equal(sliceText(undefined, 251), '');
    assert.equal(sliceText(0, undefined), '');
    assert.equal(sliceText(NaN, NaN), '');
});

test('the panel labels match the reference layout', () => {
    assert.deepEqual(PANEL_LABELS, { axial: 'Ax', sagittal: 'Sag', coronal: 'Cor', render: '3D' });
});

// ---------------------------------------------------------------------------
// The DOM half
// ---------------------------------------------------------------------------

function fakeElement() {
    const doc = {
        createElement: () => ({
            className: '',
            textContent: '',
            children: [],
            appendChild(node) {
                this.children.push(node);
            },
            remove() {
                this.removed = true;
            },
        }),
    };
    return {
        ownerDocument: doc,
        children: [],
        querySelector: () => null,
        appendChild(node) {
            this.children.push(node);
        },
    };
}

test('the overlay carries the panel name and four edges', () => {
    const element = fakeElement();
    const nodes = createOverlay(element, { orientation: 'sagittal' });

    assert.equal(nodes.panel.textContent, 'Sag');
    assert.deepEqual(Object.keys(nodes.edges).sort(), ['bottom', 'left', 'right', 'top']);
    assert.equal(element.children.length, 1, 'one root, appended to the window');
    assert.equal(nodes.root.className, OVERLAY_CLASSES.root);
});

test('updating writes the letters, the slice and the window readout', () => {
    const nodes = createOverlay(fakeElement(), { orientation: 'axial' });
    updateOverlay(nodes, {
        camera: PRESETS.axial,
        sliceIndex: 114,
        sliceCount: 251,
        windowText: 'W 2800 / L 800',
        utilities: UTILITIES,
    });

    assert.equal(nodes.edges.left.textContent, 'R');
    assert.equal(nodes.edges.right.textContent, 'L');
    assert.equal(nodes.slice.textContent, '115 / 251');
    assert.equal(nodes.window.textContent, 'W 2800 / L 800');
});

test('updating a missing overlay is a no-op', () => {
    assert.doesNotThrow(() => updateOverlay(null, { utilities: UTILITIES }));
});

test('an unknown orientation gets no panel name rather than "undefined"', () => {
    const nodes = createOverlay(fakeElement(), { orientation: 'oblique' });
    assert.equal(nodes.panel.textContent, '');
});
