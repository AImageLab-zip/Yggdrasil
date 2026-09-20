import test from 'node:test';
import assert from 'node:assert/strict';

import {
    countsByType,
    cssColor,
    markersFor,
} from '../imaging/mesh/landmarkMarkers.js';
import { emptyDocument, place } from '../imaging/mesh/landmarkDocument.js';
import {
    MESH_CONTROL_IDS,
    controlPlan,
    hexColor,
    instructionFor,
    parseHexColor,
    setEyeIcon,
    setPressed,
} from '../imaging/mesh/meshControls.js';
import { GRID_SIZES, gridLines } from '../imaging/mesh/screenGrid.js';
import { JAW_COLORS } from '../imaging/mesh/meshViewport.js';

function fixture() {
    const document = emptyDocument();
    place(document, { jaw: 'upper', tooth: '11', type: 'incisal', point: [1, 1, 1] });
    place(document, { jaw: 'upper', tooth: '11', type: 'cusps', point: [2, 2, 2] });
    place(document, { jaw: 'lower', tooth: '31', type: 'gingival', point: [3, 3, 3] });
    return document;
}

// ---------------------------------------------------------------------------
// Marker visibility: three conditions, ANDed
// ---------------------------------------------------------------------------

test('landmarks are hidden entirely when the layer is off', () => {
    // The legacy viewer showed them passively, so a switch reading "off" sat over visible
    // outlines. Phase 5 fixed the same asymmetry on the photo surface.
    assert.deepEqual(markersFor(fixture(), { visible: false }), []);
});

test('hiding an arch hides its landmarks', () => {
    // The condition that is obvious when wrong: the upper jaw's points would otherwise
    // float in space over the lower.
    const markers = markersFor(fixture(), { jawVisible: { upper: false, lower: true } });
    assert.deepEqual(markers.map((marker) => marker.jaw), ['lower']);
});

test('a type can be switched off on its own, and absent reads as visible', () => {
    const hidden = markersFor(fixture(), { typeVisible: { cusps: false } });
    assert.ok(!hidden.some((marker) => marker.type === 'cusps'));
    assert.equal(markersFor(fixture(), { typeVisible: {} }).length, 3);
});

test('the selected marker is larger and fully opaque', () => {
    const selected = { jaw: 'upper', tooth: '11', type: 'incisal', index: null };
    const [marker] = markersFor(fixture(), { selected }).filter((entry) => entry.selected);
    assert.equal(marker.opacity, 1);
    assert.ok(marker.radius > 0.65);
});

test('marker size scales every marker', () => {
    const markers = markersFor(fixture(), { markerSize: 2 });
    assert.ok(markers.every((marker) => marker.radius === 2));
});

test('per-type counts cover every type, including the empty ones', () => {
    const counts = countsByType(fixture());
    assert.equal(counts.incisal, 1);
    assert.equal(counts.cusps, 1);
    assert.equal(counts.planar, 0);
});

test('colours render as css hex', () => {
    assert.equal(cssColor('incisal'), '#f97316');
    assert.equal(cssColor('nonsense'), '#ffffff');
});

// ---------------------------------------------------------------------------
// The template interface
// ---------------------------------------------------------------------------

test('a missing control degrades rather than throwing', () => {
    // `ios.js` bound `toggleLandmarkMode` unguarded while the element sat behind a
    // template condition, so a project with landmarks switched off threw here and took
    // the reset, wireframe, grid and all seven camera buttons down with it.
    const plan = controlPlan({ getElementById: () => null, querySelectorAll: () => [] });
    assert.equal(plan.landmarkMode, null);
    assert.deepEqual(plan.gridSizeButtons, []);
});

test('every declared control id is resolved', () => {
    const seen = [];
    const plan = controlPlan({
        getElementById: (id) => { seen.push(id); return { id }; },
        querySelectorAll: () => [],
    });
    assert.deepEqual(seen.sort(), Object.values(MESH_CONTROL_IDS).sort());
    assert.equal(plan.viewport.id, 'scan-viewer');
});

test('the instruction line says exactly what a click will do', () => {
    assert.equal(instructionFor({ active: false }), '');
    assert.equal(instructionFor({ active: true, canEdit: false }), 'Viewing saved landmarks');
    assert.equal(instructionFor({ active: true, canEdit: true, tooth: '' }), 'Select an FDI tooth');
    assert.match(
        instructionFor({ active: true, canEdit: true, tooth: '11', type: null }),
        /Select a landmark type/,
    );
    assert.match(
        instructionFor({ active: true, canEdit: true, tooth: '11', type: 'Incisal' }),
        /Shift \+ left-click/,
    );
});

test('pressed state sets the class and aria together', () => {
    const calls = [];
    const element = {
        classList: { toggle: (name, on) => calls.push([name, on]) },
        setAttribute: (name, value) => calls.push([name, value]),
    };
    setPressed(element, true);
    assert.deepEqual(calls, [['active', true], ['aria-pressed', 'true']]);
});

test('the eye icon swaps both classes, so it cannot show two states', () => {
    const classes = new Set(['fa-eye']);
    const icon = { classList: { toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)) } };
    setEyeIcon({ querySelector: () => icon }, false);
    assert.ok(classes.has('fa-eye-slash'));
    assert.ok(!classes.has('fa-eye'));
});

// ---------------------------------------------------------------------------
// The screen grid
// ---------------------------------------------------------------------------

test('a grid draws its border as well as its divisions', () => {
    // N+1 lines each way. A grid missing its border reads as misaligned.
    for (const size of GRID_SIZES) {
        assert.equal(gridLines(size, 100, 100).length, (size + 1) * 2);
    }
});

test('a zero-sized host produces no lines rather than NaNs', () => {
    assert.deepEqual(gridLines(9, 0, 0), []);
});

test('the two arches separate by lightness, not only by hue', () => {
    /**
     * The first pair separated by hue alone -- 1.17:1 in relative luminance, which is to
     * say not at all once vtk shades them. Two surfaces meeting at the occlusal plane are
     * read through lighting that varies far more than that, so the arches looked like one
     * object.
     *
     * A value split is what survives the shading, a greyscale screenshot, and a red-green
     * colour vision deficiency. Pinned as a floor rather than as exact colours, so the
     * palette can be re-picked without editing a test -- but not flattened again.
     */
    const relativeLuminance = ([red, green, blue]) => {
        const channel = (value) => {
            const c = value / 255;
            return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
        };
        return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue);
    };

    const upper = relativeLuminance(JAW_COLORS.upper);
    const lower = relativeLuminance(JAW_COLORS.lower);
    const contrast =
        (Math.max(upper, lower) + 0.05) / (Math.min(upper, lower) + 0.05);
    assert.ok(contrast >= 2, `the arches are only ${contrast.toFixed(2)}:1 apart in value`);

    // The arches also differ in hue -- one warm, one cool -- so the two cues agree rather
    // than one carrying the whole distinction.
    const [upperRed, , upperBlue] = JAW_COLORS.upper;
    const [lowerRed, , lowerBlue] = JAW_COLORS.lower;
    assert.ok(upperRed > upperBlue, 'the upper arch is not the warm one');
    assert.ok(lowerBlue > lowerRed, 'the lower arch is not the cool one');

    // Neither so saturated that it reads as a finding or competes with the landmark
    // markers sitting on it -- the type palette already spends red, orange, blue and
    // purple, and those markers are small.
    for (const jaw of ['upper', 'lower']) {
        const [red, green, blue] = JAW_COLORS[jaw];
        const spread = Math.max(red, green, blue) - Math.min(red, green, blue);
        assert.ok(spread <= 110, `${jaw} is too saturated for a clinical surface`);
    }
});


// ---------------------------------------------------------------------------
// Jaw colour: the hex the controls speak, and the triples the viewport takes
// ---------------------------------------------------------------------------

test('every jaw default survives the round trip through hex', () => {
    // `<input type="color">` only accepts `#rrggbb`, so the defaults reach the picker
    // through `hexColor` and come back through `parseHexColor` on the first drag. A lossy
    // trip would shift both arches the moment the menu was opened.
    for (const jaw of Object.keys(JAW_COLORS)) {
        assert.deepEqual(parseHexColor(hexColor(JAW_COLORS[jaw])), [...JAW_COLORS[jaw]]);
    }
});

test('hex is lowercase and zero-padded, which is the only form the picker accepts', () => {
    // A channel below 16 is where a missing pad shows up: `#0f8` is five digits, and the
    // input silently keeps its old value rather than reporting anything.
    assert.equal(hexColor([0, 15, 136]), '#000f88');
});

test('a typed hex is read with or without the hash, in either case', () => {
    assert.deepEqual(parseHexColor('#4E70A0'), [78, 112, 160]);
    assert.deepEqual(parseHexColor('4e70a0'), [78, 112, 160]);
    assert.deepEqual(parseHexColor('  #4e70a0  '), [78, 112, 160]);
});

test('three-digit shorthand expands', () => {
    assert.deepEqual(parseHexColor('#f00'), [255, 0, 0]);
});

test('text that is not a colour is null, never black', () => {
    // The distinction the hex field is built on: `null` means "leave the arch alone and
    // put the old value back", and returning [0,0,0] here would paint a jaw black on a
    // typo or halfway through typing.
    for (const junk of ['', '#', 'blue', '#12', '#12345', '#1234567', '#gggggg', null]) {
        assert.equal(parseHexColor(junk), null, JSON.stringify(junk));
    }
});

test('the colour controls are registered so the template ids cannot drift', () => {
    // `maxillo/tests_ios_surface.py` asserts the other half of this pair.
    assert.equal(MESH_CONTROL_IDS.upperColor, 'iosUpperJawColor');
    assert.equal(MESH_CONTROL_IDS.upperColorHex, 'iosUpperJawColorHex');
    assert.equal(MESH_CONTROL_IDS.lowerColor, 'iosLowerJawColor');
    assert.equal(MESH_CONTROL_IDS.lowerColorHex, 'iosLowerJawColorHex');
});
