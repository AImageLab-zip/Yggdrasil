/**
 * The tooth grid's model.
 *
 * The grid is the only place in the editor where a polygon gets its FDI code, and the code
 * decides which segment it is exported under -- so the mappings below are not cosmetic.
 * What is asserted is everything that could be silently wrong: the arch's colour order,
 * the borrowed and mirrored icons, and the only-selected filter.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { FDI_CODES, MOUTH_ORDER } from '../imaging/photos/labelMapper.js';
import {
    PALETTE,
    confirmControl,
    gradientColor,
    interpolateHex,
    normalizeToothSvg,
    onlySelectedControl,
    polygonCount,
    toothButtons,
    toothColor,
    toothIconMirrored,
    toothIconSource,
} from '../imaging/photos/toothGrid.js';

const SQUARE = [
    [10, 10],
    [30, 10],
    [30, 30],
];

describe('toothColor', () => {
    it('runs continuously across each arch rather than mirroring at the midline', () => {
        // 18 -> 11 -> 21 -> 28 is one sweep, so the two central incisors are adjacent
        // colours and the two second molars are the extremes. A mirrored gradient would
        // give 18 and 28 the same colour, which is the one pair a clinician must never
        // confuse.
        assert.equal(toothColor('18'), PALETTE[0]);
        assert.equal(toothColor('28'), PALETTE.at(-1));
        assert.notEqual(toothColor('18'), toothColor('28'));
        assert.equal(toothColor('48'), toothColor('18'), 'the lower arch repeats the upper');
        assert.equal(toothColor('38'), toothColor('28'));
    });

    it('gives all 32 teeth a colour, and adjacent teeth different ones', () => {
        const upper = MOUTH_ORDER.slice(0, 16).map(toothColor);
        assert.equal(new Set(upper).size, 16, 'no two teeth in an arch share a colour');
        for (const code of FDI_CODES) {
            assert.match(toothColor(code), /^#[0-9a-f]{6}$/);
        }
    });

    it('falls back rather than throwing on a code that is not a tooth', () => {
        // The grid is built from MOUTH_ORDER so this cannot happen from the UI, but a
        // colour lookup that threw would take the whole grid down for one bad key.
        assert.equal(toothColor('99'), PALETTE[0]);
        assert.equal(toothColor(undefined), PALETTE[0]);
    });
});

describe('interpolateHex and gradientColor', () => {
    it('hits both ends exactly', () => {
        assert.equal(interpolateHex('#000000', '#ffffff', 0), '#000000');
        assert.equal(interpolateHex('#000000', '#ffffff', 1), '#ffffff');
        assert.equal(interpolateHex('#000000', '#ffffff', 0.5), '#808080');
    });

    it('a single-entry gradient is the first colour, not a division by zero', () => {
        assert.equal(gradientColor(0, 1), PALETTE[0]);
        assert.equal(gradientColor(0, 0), PALETTE[0]);
    });
});

describe('toothIconSource', () => {
    it('borrows the patient-right icon for the patient-left quadrants', () => {
        assert.equal(toothIconSource('21'), '11');
        assert.equal(toothIconSource('26'), '16');
        assert.equal(toothIconSource('41'), '31');
    });

    it('keeps the two substitutions the icon set has no files for', () => {
        // 37 and 47 have no icon of their own. Left to fall through, 37 would ask for
        // `37.svg` and render an empty button; a nearest-neighbour guess would put a
        // molar's outline on the wrong tooth.
        assert.equal(toothIconSource('37'), '36');
        assert.equal(toothIconSource('47'), '36');
    });

    it('leaves the quadrants that own their icons alone', () => {
        assert.equal(toothIconSource('16'), '16');
        assert.equal(toothIconSource('36'), '36');
    });

    it('mirrors exactly the two borrowed quadrants', () => {
        for (const code of FDI_CODES) {
            assert.equal(
                toothIconMirrored(code),
                code[0] === '2' || code[0] === '4',
                code
            );
        }
    });
});

describe('normalizeToothSvg', () => {
    it('rewrites the hardcoded fill so the button can tint itself', () => {
        const out = normalizeToothSvg('<svg><path fill="#B2F2BB" d="M0 0"/></svg>');
        assert.match(out, /fill="currentColor"/);
        assert.doesNotMatch(out, /b2f2bb/i);
    });

    it('strips the prolog and doctype, which are illegal inline', () => {
        const out = normalizeToothSvg('<?xml version="1.0"?><!DOCTYPE svg><svg></svg>');
        assert.doesNotMatch(out, /<\?xml|DOCTYPE/);
        assert.match(out, /^<svg aria-hidden="true" focusable="false"/);
    });
});

describe('toothButtons', () => {
    const teeth = { 36: [SQUARE, SQUARE], 11: [SQUARE] };

    it('is 32 buttons in mouth order, with per-tooth counts', () => {
        const buttons = toothButtons({ teeth });
        assert.equal(buttons.length, 32);
        assert.deepEqual(
            buttons.map((button) => button.code),
            [...MOUTH_ORDER]
        );
        const byCode = new Map(buttons.map((button) => [button.code, button]));
        assert.equal(byCode.get('36').count, 2);
        assert.equal(byCode.get('11').count, 1);
        assert.equal(byCode.get('17').count, 0);
    });

    it('hides the other teeth only while one is selected', () => {
        const withSelection = toothButtons({ teeth, selected: '36', onlySelected: true });
        assert.equal(withSelection.filter((button) => !button.hidden).length, 1);

        // With nothing selected the filter must be inert. Hiding all 32 would look like a
        // viewer that had lost its grid.
        const withoutSelection = toothButtons({ teeth, selected: null, onlySelected: true });
        assert.equal(withoutSelection.filter((button) => button.hidden).length, 0);
    });

    it('marks a read-only grid disabled without hiding it', () => {
        const buttons = toothButtons({ teeth, editable: false });
        assert.ok(buttons.every((button) => button.disabled));
        assert.ok(buttons.every((button) => !button.hidden));
    });
});

describe('polygonCount', () => {
    it('is zero for a missing or malformed entry rather than NaN', () => {
        assert.equal(polygonCount({}, '11'), 0);
        assert.equal(polygonCount(undefined, '11'), 0);
        assert.equal(polygonCount({ 11: 'nope' }, '11'), 0);
    });
});

describe('the two toolbar controls', () => {
    it('only-selected needs both an image and a selection', () => {
        assert.equal(
            onlySelectedControl({ onlySelected: false, hasImage: true, selected: '36' }).disabled,
            false
        );
        assert.equal(
            onlySelectedControl({ onlySelected: false, hasImage: true, selected: null }).disabled,
            true
        );
        assert.equal(
            onlySelectedControl({ onlySelected: true, hasImage: true, selected: '36' }).label,
            'Show all'
        );
    });

    it('confirm reads Reopen once confirmed, and is disabled for a reader', () => {
        assert.equal(
            confirmControl({ confirmed: true, hasImage: true, canModify: true }).label,
            'Reopen'
        );
        assert.equal(
            confirmControl({ confirmed: false, hasImage: true, canModify: true }).label,
            'Mark done'
        );
        assert.equal(
            confirmControl({ confirmed: false, hasImage: true, canModify: false }).disabled,
            true
        );
    });
});
