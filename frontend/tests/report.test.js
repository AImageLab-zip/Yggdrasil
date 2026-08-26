import test from 'node:test';
import assert from 'node:assert/strict';

import {
    BLOCKING_TIERS,
    VERDICT,
    formatReport,
    formatRun,
    summarize,
    verdictFor,
} from '../imaging/validation/report.js';

const PASSING_TIER1 = { passed: true, seed: 1, sampleCount: 10, legs: [], warnings: [] };
const PASSING_TIER2 = {
    passed: true,
    notes: [],
    voxels: { passed: true, samples: 10, failures: 0, maxDeviation: 0, residualLut: { rescaleSlope: 1, rescaleIntercept: 0 }, upstreamApplied: true, error: null },
    distributions: { passed: true, issues: [] },
};

function run(overrides = {}) {
    return { study: 'patient-1', tier1: PASSING_TIER1, tier2: PASSING_TIER2, ...overrides };
}

test('tier 3 is not a gate -- only tiers 1 and 2 block', () => {
    assert.deepEqual(BLOCKING_TIERS, [1, 2]);
});

test('a study passing both gates passes', () => {
    const outcome = verdictFor(run());
    assert.equal(outcome.verdict, VERDICT.PASSED);
    assert.deepEqual(outcome.blocking, []);
});

test('an unreviewed tier 3 holds the verdict at awaiting-review, not passed', () => {
    // Appearance is human-reviewed. A run that has produced a contact sheet nobody has
    // looked at yet has not cleared anything.
    const outcome = verdictFor(run({ tier3: { reviewed: false } }));
    assert.equal(outcome.verdict, VERDICT.AWAITING_REVIEW);
    assert.deepEqual(outcome.blocking, [], 'and it is still not *blocking*');

    assert.equal(verdictFor(run({ tier3: { reviewed: true } })).verdict, VERDICT.PASSED);
});

test('a failing gate names the tier and its own blocking reasons', () => {
    const outcome = verdictFor(
        run({ tier1: { ...PASSING_TIER1, passed: false, blocking: ['cornerstone'] } })
    );
    assert.equal(outcome.verdict, VERDICT.FAILED);
    assert.match(outcome.blocking.join(' '), /tier 1 failed \(cornerstone\)/);
});

test('a tier that did not run is a failure, not an absence', () => {
    // The failure mode this prevents: a harness that throws before tier 2 and reports
    // "tier 1 passed" as though that were the whole gate.
    const outcome = verdictFor({ study: 'patient-1', tier1: PASSING_TIER1 });
    assert.equal(outcome.verdict, VERDICT.FAILED);
    assert.match(outcome.blocking.join(' '), /tier 2 did not run/);
});

test('a study that could not be loaded is errored, never skipped', () => {
    const outcome = verdictFor({ study: 'patient-9', error: 'HTTP 404' });
    assert.equal(outcome.verdict, VERDICT.ERRORED);
    assert.match(outcome.blocking.join(' '), /could not be loaded: HTTP 404/);
});

test('warnings travel with a passing run rather than being dropped', () => {
    const outcome = verdictFor(
        run({
            tier1: { ...PASSING_TIER1, warnings: ['declares no orientation (F2)'] },
            tier2: { ...PASSING_TIER2, notes: ['live instance of F1'] },
        })
    );
    assert.equal(outcome.verdict, VERDICT.PASSED);
    assert.equal(outcome.warnings.length, 2);
    assert.match(outcome.warnings.join(' '), /F2/);
    assert.match(outcome.warnings.join(' '), /F1/);
});

test('an empty corpus does not clear the gate', () => {
    // "Green across the maxillo and brain corpora" is satisfied vacuously by zero
    // studies, which is exactly the reading that must not be available.
    const summary = summarize([]);
    assert.equal(summary.verdict, VERDICT.FAILED);
    assert.equal(summary.mayMerge, false);
    assert.match(summary.blocking.join(' '), /empty corpus/);
});

test('one errored study fails the whole corpus', () => {
    const summary = summarize([run(), run({ study: 'p2' }), { study: 'p3', error: 'timeout' }]);
    assert.equal(summary.verdict, VERDICT.FAILED);
    assert.equal(summary.mayMerge, false);
    assert.deepEqual(summary.counts, { passed: 2, failed: 0, 'awaiting-review': 0, errored: 1 });
});

test('awaiting review is not permission to merge', () => {
    const summary = summarize([run(), run({ study: 'p2', tier3: { reviewed: false } })]);
    assert.equal(summary.verdict, VERDICT.AWAITING_REVIEW);
    assert.equal(summary.mayMerge, false);
});

test('mayMerge is true only when every study passed and there was at least one', () => {
    const summary = summarize([run(), run({ study: 'p2' })]);
    assert.equal(summary.verdict, VERDICT.PASSED);
    assert.equal(summary.mayMerge, true);
    assert.equal(summary.total, 2);
});

test('the formatted report leads with the verdict and the merge answer', () => {
    const text = formatReport([run(), { study: 'p3', error: 'timeout' }]);
    assert.match(text, /^Phase 3 validation harness: FAILED/);
    assert.match(text, /may merge: NO/);
    assert.match(text, /Blocking:/);
    assert.match(text, /p3 could not be loaded: timeout/);
});

test('a formatted run surfaces the residual LUT and whether upstream skipped', () => {
    // The two facts a reviewer needs to interpret a green tier 2: which LUT was
    // outstanding, and whether this study is a live instance of F1.
    const text = formatRun(
        run({
            tier2: {
                ...PASSING_TIER2,
                voxels: {
                    ...PASSING_TIER2.voxels,
                    residualLut: { rescaleSlope: 1, rescaleIntercept: -1024 },
                    upstreamApplied: false,
                },
            },
        })
    );
    assert.match(text, /residual LUT \(1, -1024\)/);
    assert.match(text, /upstream SKIPPED the rescale/);
});
