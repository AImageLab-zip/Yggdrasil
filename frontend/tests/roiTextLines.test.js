import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
    AREA_TOOLS,
    areaOnlyConfiguration,
    createAreaOnlyTextLines,
} from '../imaging/annotations/roiTextLines.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..', '..');

const roundNumber = (value) => String(Math.round(value * 100) / 100);
const getTextLines = createAreaOnlyTextLines(roundNumber);

function data(stats) {
    return { cachedStats: { target: stats } };
}

test('only the area is drawn', () => {
    // Upstream prints Area, Mean, Max, Min and Std Dev. On a photograph those four are
    // statistics about the JPEG; on a CBCT they are not Hounsfield, and this codebase
    // already refuses to *store* them from the client for that reason. Printing them while
    // refusing to store them is one claim made in two voices.
    const lines = getTextLines(
        data({ area: 12.3456, areaUnit: 'mm²', mean: 143, max: 255, min: 0, stdDev: 40 }),
        'target'
    );
    assert.deepEqual(lines, ['Area: 12.35 mm²']);
    const text = lines.join(' ');
    for (const banned of ['Mean', 'Max', 'Min', 'Std']) {
        assert.ok(!text.includes(banned), `${banned} must not be drawn`);
    }
});

test('the unit comes from the stats, so calibration is reflected without asking', () => {
    assert.deepEqual(getTextLines(data({ area: 10, areaUnit: 'px²' }), 'target'), ['Area: 10 px²']);
    assert.deepEqual(getTextLines(data({ area: 10, areaUnit: 'mm² User' }), 'target'), [
        'Area: 10 mm² User',
    ]);
});

test('a missing unit does not leave a trailing space', () => {
    assert.deepEqual(getTextLines(data({ area: 10 }), 'target'), ['Area: 10']);
});

test('an ROI with no area yet draws nothing', () => {
    // Returning `undefined` is upstream's "no text box": the shape is still being drawn,
    // or its stats have not been computed. An empty array would draw an empty box.
    assert.equal(getTextLines(data({}), 'target'), undefined);
    assert.equal(getTextLines(data({ mean: 143 }), 'target'), undefined);
    assert.equal(getTextLines({ cachedStats: {} }, 'target'), undefined);
    assert.equal(getTextLines({}, 'target'), undefined);
});

test('a non-finite area draws nothing rather than NaN', () => {
    for (const area of [NaN, Infinity, null, '10']) {
        assert.equal(getTextLines(data({ area, areaUnit: 'mm²' }), 'target'), undefined, String(area));
    }
});

test('several target ids are accepted, and the first with an area wins', () => {
    const multi = { cachedStats: { a: { mean: 1 }, b: { area: 5, areaUnit: 'mm²' } } };
    assert.deepEqual(getTextLines(multi, ['a', 'b']), ['Area: 5 mm²']);
});

test('the configuration covers exactly the three area tools', () => {
    const tools = {
        Length: { toolName: 'Length' },
        RectangleROI: { toolName: 'RectangleROI' },
        EllipticalROI: { toolName: 'EllipticalROI' },
        CircleROI: { toolName: 'CircleROI' },
    };
    const configuration = areaOnlyConfiguration(tools, roundNumber);
    assert.deepEqual([...configuration.keys()].sort(), [...AREA_TOOLS].sort());
    assert.ok(!configuration.has('Length'), 'Length prints one line already');
    for (const value of configuration.values()) {
        assert.equal(typeof value.getTextLines, 'function');
    }
});

test('a tool the surface does not bind is skipped', () => {
    const configuration = areaOnlyConfiguration({ RectangleROI: { toolName: 'RectangleROI' } }, roundNumber);
    assert.deepEqual([...configuration.keys()], ['RectangleROI']);
});

test('upstream still reads getTextLines from the configuration', () => {
    // The whole mechanism is one config key. If a version bump renames it, the ROI tools
    // silently go back to five lines, so it is asserted against the shipped source.
    const source = readFileSync(
        join(REPO, 'node_modules', '@cornerstonejs', 'tools', 'dist', 'esm', 'tools', 'annotation', 'RectangleROITool.js'),
        'utf8'
    );
    assert.match(source, /this\.configuration\.getTextLines\(/);
    assert.match(source, /getTextLines: defaultAreaGetTextLines/);
});

test('upstream default really does print the four extra metrics', () => {
    // Evidence for the change rather than an assumption about it.
    const source = readFileSync(
        join(REPO, 'node_modules', '@cornerstonejs', 'tools', 'dist', 'esm', 'utilities', 'defaultGetTextLines.js'),
        'utf8'
    );
    for (const metric of ['Mean', 'Max', 'Min', 'Std Dev']) {
        assert.ok(source.includes(`'${metric}'`), `${metric} is in upstream's AREA_METRICS`);
    }
});
