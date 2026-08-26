/**
 * A seeded pseudo-random generator, because a gate may not be non-deterministic.
 *
 * Tier 1 of the Phase 3 harness samples ~10^4 voxel indices per study. If those
 * indices came from `Math.random()`, a run that passed could not be reproduced, a run
 * that failed could not be re-examined at the index that failed, and "the harness is
 * green" would mean "the harness was green once, on samples nobody can name". With no
 * feature flags, this harness *is* the safety net for deleting four viewers -- it has
 * to be re-runnable.
 *
 * mulberry32: 32-bit state, one multiply-xorshift round, uniform enough for choosing
 * sample points and small enough to read. It is not cryptographic and must never be
 * used as though it were.
 */

/**
 * @param {number} seed any 32-bit integer.
 * @returns {() => number} successive values in [0, 1).
 */
export function mulberry32(seed) {
    let state = seed >>> 0;
    return function next() {
        state = (state + 0x6d2b79f5) >>> 0;
        let t = state;
        t = Math.imul(t ^ (t >>> 15), t | 1);
        t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

/**
 * The seed the harness uses unless a run overrides it.
 *
 * Fixed and committed on purpose: two runs of the harness, on two machines, against
 * the same study, must sample the same voxels, or the Tier 1 numbers in one run's
 * report cannot be compared with another's.
 */
export const DEFAULT_SEED = 0x59474744; // 'YGGD'
