import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
    FDI_CODES,
    MOUTH_ORDER,
    SCHEMA_SLUG,
    SCHEMA_VERSION,
    assertMatchesSchema,
    fdiCodeFor,
    isFdiCode,
    segmentIndexFor,
} from '../imaging/photos/labelMapper.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..', '..');

// ---------------------------------------------------------------------------
// Exhaustive, because the roadmap says this table is load-bearing
// ---------------------------------------------------------------------------

test('all 32 codes map to 1..32 with no gaps and no collisions', () => {
    // A wrong entry does not throw. It relabels a tooth, in an export, silently -- which
    // is why this is asserted over the whole table rather than sampled.
    assert.equal(FDI_CODES.length, 32);
    const values = FDI_CODES.map(segmentIndexFor);
    assert.deepEqual(values, Array.from({ length: 32 }, (_, index) => index + 1));
    assert.equal(new Set(values).size, 32);
});

test('the mapping round-trips in both directions for every tooth', () => {
    for (const code of FDI_CODES) {
        assert.equal(fdiCodeFor(segmentIndexFor(code)), code);
    }
    for (let index = 1; index <= 32; index += 1) {
        assert.equal(segmentIndexFor(fdiCodeFor(index)), index);
    }
});

test('the formula is the one the migration froze', () => {
    // annotations/migrations/0002 assigns value sequentially over quadrant then position,
    // and UniqueConstraint(schema, value) is what makes "an integer 2 in an old labelmap
    // must never change meaning" a guarantee. A second numbering here would be a second
    // source of truth for a value frozen in DDL.
    const source = readFileSync(
        join(REPO, 'annotations', 'migrations', '0002_seed_fdi_schema.py'),
        'utf8'
    );
    assert.match(source, /SCHEMA_SLUG = "fdi-permanent"/);
    assert.match(source, /SCHEMA_VERSION = 1/);
    assert.equal(SCHEMA_SLUG, 'fdi-permanent');
    assert.equal(SCHEMA_VERSION, 1);
    // The seed iterates sorted quadrants then sorted positions, incrementing by one.
    assert.match(source, /for quadrant in sorted\(_QUADRANTS\)/);
    assert.match(source, /for position in sorted\(_POSITIONS\)/);
    assert.match(source, /value \+= 1/);

    // Spot values that pin the ordering itself: quadrant-major, so 21 is 9 and not 2.
    assert.equal(segmentIndexFor('11'), 1);
    assert.equal(segmentIndexFor('18'), 8);
    assert.equal(segmentIndexFor('21'), 9);
    assert.equal(segmentIndexFor('31'), 17);
    assert.equal(segmentIndexFor('41'), 25);
    assert.equal(segmentIndexFor('48'), 32);
});

test('mouth order and storage order are the same set in different orders', () => {
    // Three orderings of these 32 teeth exist in the codebase, and this is the pair that
    // bites: anything indexing an array would be right for the upper right quadrant and
    // wrong for the rest, which passes a spot check.
    assert.deepEqual([...MOUTH_ORDER].sort(), [...FDI_CODES].sort());
    assert.notDeepEqual(MOUTH_ORDER, FDI_CODES, 'if these ever match, the test is vacuous');
    assert.notEqual(
        MOUTH_ORDER.indexOf('48'),
        FDI_CODES.indexOf('48'),
        'the lower arch is where the two orders disagree'
    );
});

test('the mouth order matches the editor being replaced', () => {
    const source = readFileSync(join(REPO, 'static', 'js', 'intraoral_segmentation.js'), 'utf8');
    const listed = [...source.matchAll(/'([1-4][1-8])'/g)].map((match) => match[1]);
    // The first 32 quoted two-digit codes in that file are its `toothCodes` array.
    assert.deepEqual(listed.slice(0, 32), [...MOUTH_ORDER]);
});

// ---------------------------------------------------------------------------
// Refusals
// ---------------------------------------------------------------------------

test('deciduous codes are refused, not given invented values', () => {
    // The seed's docstring is explicit: they would need their own schema, and inventing
    // values here would freeze a numbering nobody has reviewed.
    for (const code of ['51', '55', '61', '71', '85']) {
        assert.equal(isFdiCode(code), false, code);
        assert.throws(() => segmentIndexFor(code), /permanent-dentition/);
    }
});

test('a malformed code is refused rather than defaulted', () => {
    // Not a default: a segment index chosen by fallback puts a polygon under the wrong
    // tooth in an export and looks fine doing it.
    for (const code of ['09', '10', '19', '40', '5', '111', '1a', '', null, 11, undefined]) {
        assert.equal(isFdiCode(code), false, String(code));
        assert.throws(() => segmentIndexFor(code));
    }
});

test('a segment index outside the range is refused', () => {
    for (const value of [0, 33, -1, 1.5, '1', null, NaN]) {
        assert.throws(() => fdiCodeFor(value), undefined, String(value));
    }
});

// ---------------------------------------------------------------------------
// Checking the projection against what the server serves
// ---------------------------------------------------------------------------

test('a schema that agrees passes', () => {
    const definitions = FDI_CODES.map((code) => ({ code, value: segmentIndexFor(code) }));
    assert.doesNotThrow(() => assertMatchesSchema(definitions));
});

test('a disagreement names every offending code, not just the first', () => {
    // The disagreement is about which integer a stored voxel means, so it must surface
    // in full rather than one code at a time across successive deploys.
    const definitions = FDI_CODES.map((code) => ({ code, value: segmentIndexFor(code) }));
    definitions[0].value = 99;
    definitions[5].value = 98;
    assert.throws(() => assertMatchesSchema(definitions), (error) => {
        assert.match(error.message, /11: schema says 99/);
        assert.match(error.message, /16: schema says 98/);
        return true;
    });
});

test('a missing tooth is reported', () => {
    const definitions = FDI_CODES.slice(1).map((code) => ({ code, value: segmentIndexFor(code) }));
    assert.throws(() => assertMatchesSchema(definitions), /11 is missing from the schema/);
});
