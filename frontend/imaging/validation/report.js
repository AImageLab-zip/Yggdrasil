/**
 * Turning tier results into a verdict a person can act on.
 *
 * The roadmap makes Tier 1 and Tier 2 gates and Tier 3 explicitly *not* a gate --
 * appearance is human-reviewed. That asymmetry has to survive into the summary, or the
 * page ends up showing one green tick that means two different things. So
 * {@link summarize} counts blocking failures separately from advisory ones, and a run
 * with only Tier 3 outstanding reports as "awaiting review", never as "passed".
 *
 * The other rule encoded here: **a study that could not be loaded is not a pass.** The
 * tempting shape is to skip it and report on the rest, which produces a green summary
 * over a corpus the harness never actually read. {@link summarize} counts errored runs
 * as blocking.
 *
 * Pure -- objects in, objects and strings out -- so the verdict logic is unit-tested
 * rather than inspected by eye on a page that needs a GPU to render.
 */

/** Tiers that block the Phase 3 merge. Tier 3 is deliberately absent. */
export const BLOCKING_TIERS = Object.freeze([1, 2]);

/** What a run can come to. */
export const VERDICT = Object.freeze({
    PASSED: 'passed',
    FAILED: 'failed',
    AWAITING_REVIEW: 'awaiting-review',
    ERRORED: 'errored',
});

/**
 * Decide one study's verdict from its tier results.
 *
 * @param {object} run
 * @param {string} run.study a human-readable name.
 * @param {object} [run.tier1]
 * @param {object} [run.tier2]
 * @param {object} [run.tier3] appearance; advisory only.
 * @param {string} [run.error] set when the study could not be loaded at all.
 * @returns {{verdict: string, blocking: string[], warnings: string[]}}
 */
export function verdictFor(run) {
    if (run.error) {
        return {
            verdict: VERDICT.ERRORED,
            blocking: [`${run.study} could not be loaded: ${run.error}`],
            warnings: [],
        };
    }

    const blocking = [];
    for (const tier of BLOCKING_TIERS) {
        const result = run[`tier${tier}`];
        if (!result) {
            blocking.push(`${run.study}: tier ${tier} did not run.`);
            continue;
        }
        if (!result.passed) {
            const detail = (result.blocking ?? []).join(', ') || 'see the tier report';
            blocking.push(`${run.study}: tier ${tier} failed (${detail}).`);
        }
    }

    const warnings = [
        ...(run.tier1?.warnings ?? []),
        ...(run.tier2?.notes ?? []),
    ].map((message) => `${run.study}: ${message}`);

    if (blocking.length) {
        return { verdict: VERDICT.FAILED, blocking, warnings };
    }
    if (run.tier3 && run.tier3.reviewed !== true) {
        return { verdict: VERDICT.AWAITING_REVIEW, blocking: [], warnings };
    }
    return { verdict: VERDICT.PASSED, blocking: [], warnings };
}

/**
 * Roll a corpus of runs up into one answer to "may Phase 3 merge?".
 *
 * @param {object[]} runs
 * @returns {{
 *   verdict: string, total: number, counts: Record<string, number>,
 *   blocking: string[], warnings: string[], mayMerge: boolean
 * }}
 */
export function summarize(runs) {
    const counts = { passed: 0, failed: 0, 'awaiting-review': 0, errored: 0 };
    const blocking = [];
    const warnings = [];

    for (const run of runs) {
        const outcome = verdictFor(run);
        counts[outcome.verdict] += 1;
        blocking.push(...outcome.blocking);
        warnings.push(...outcome.warnings);
    }

    let verdict = VERDICT.PASSED;
    if (counts.failed || counts.errored) {
        verdict = VERDICT.FAILED;
    } else if (counts['awaiting-review']) {
        verdict = VERDICT.AWAITING_REVIEW;
    } else if (runs.length === 0) {
        // An empty corpus is not a green light. The gate is "green across the maxillo
        // *and* brain corpora"; zero studies satisfies that vacuously and must not.
        verdict = VERDICT.FAILED;
        blocking.push('No studies were run: an empty corpus cannot clear the gate.');
    }

    return {
        verdict,
        total: runs.length,
        counts,
        blocking,
        warnings,
        // The one boolean the roadmap's gate turns on. Deliberately strict: awaiting
        // review is not permission to merge.
        mayMerge: verdict === VERDICT.PASSED && runs.length > 0,
    };
}

/**
 * Render one run as plain text, for the page and for pasting into a PR.
 *
 * @param {object} run
 * @returns {string}
 */
export function formatRun(run) {
    const outcome = verdictFor(run);
    const lines = [`${run.study}: ${outcome.verdict.toUpperCase()}`];

    if (run.error) {
        lines.push(`  error: ${run.error}`);
        return lines.join('\n');
    }

    if (run.tier1) {
        lines.push(`  tier 1 (geometry): ${run.tier1.passed ? 'pass' : 'FAIL'}` +
            `  seed=${run.tier1.seed} samples=${run.tier1.sampleCount ?? 0}`);
        for (const leg of run.tier1.legs ?? []) {
            lines.push(
                `    ${leg.name}: max ${formatNumber(leg.positions.maxDeviationMm)} mm` +
                    ` over ${leg.positions.samples} samples` +
                    `, ${leg.positions.failures} outside tolerance` +
                    `, lengths ${leg.lengths.passed ? 'ok' : 'FAIL'}`
            );
            for (const issue of leg.derived.issues ?? []) {
                lines.push(`      ! ${issue}`);
            }
            if (leg.positions.error) {
                lines.push(`      ! ${leg.positions.error}`);
            }
        }
    }

    if (run.tier2) {
        const { voxels, distributions } = run.tier2;
        lines.push(`  tier 2 (intensity): ${run.tier2.passed ? 'pass' : 'FAIL'}`);
        if (voxels.error) {
            lines.push(`    ! ${voxels.error}`);
        } else {
            lines.push(
                `    voxels: max deviation ${formatNumber(voxels.maxDeviation)}` +
                    ` over ${voxels.samples} samples, ${voxels.failures} failing` +
                    `, residual LUT ${formatLut(voxels.residualLut)}` +
                    `, upstream ${voxels.upstreamApplied ? 'applied' : 'SKIPPED'} the rescale`
            );
        }
        for (const issue of distributions?.issues ?? []) {
            lines.push(`    ! ${issue}`);
        }
    }

    if (run.tier3) {
        lines.push(
            `  tier 3 (appearance): ${run.tier3.reviewed ? 'reviewed' : 'awaiting human review'}` +
                ' -- advisory, never a gate'
        );
    }

    for (const warning of outcome.warnings) {
        lines.push(`  warning: ${warning.slice(run.study.length + 2)}`);
    }

    return lines.join('\n');
}

/**
 * Render a whole corpus, verdict first.
 *
 * @param {object[]} runs
 * @returns {string}
 */
export function formatReport(runs) {
    const summary = summarize(runs);
    const header = [
        `Phase 3 validation harness: ${summary.verdict.toUpperCase()}`,
        `${summary.total} studies -- ` +
            `${summary.counts.passed} passed, ${summary.counts.failed} failed, ` +
            `${summary.counts['awaiting-review']} awaiting review, ${summary.counts.errored} errored`,
        `may merge: ${summary.mayMerge ? 'yes' : 'NO'}`,
        '',
    ];
    if (summary.blocking.length) {
        header.push('Blocking:', ...summary.blocking.map((item) => `  - ${item}`), '');
    }
    return [...header, ...runs.map(formatRun)].join('\n');
}

function formatNumber(value) {
    if (!Number.isFinite(value)) {
        return String(value);
    }
    return value === 0 ? '0' : value.toExponential(3);
}

function formatLut(lut) {
    if (!lut) {
        return 'unknown';
    }
    return `(${lut.rescaleSlope}, ${lut.rescaleIntercept})`;
}
