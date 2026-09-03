import test from 'node:test';
import assert from 'node:assert/strict';

import {
    DEFAULT_ROBUST_PERCENTILES,
    HISTOGRAM_BINS,
    MINIMUM_WINDOW_WIDTH,
    MODALITY_PRESETS,
    autoVoi,
    hasAbsolutePresets,
    presetVoi,
    robustRange,
    scalarRange,
    voiFromLimits,
    voiFromRange,
    volumeRange,
} from '../imaging/windowing/autoVoi.js';

// ---------------------------------------------------------------------------
// The ported `volumeRange` cases.
//
// `static/js/tests/niivue_render_modes.test.js` dies with NiiVue in this phase, but
// its two volumeRange assertions encode a fallback chain that took real studies to get
// right, so the roadmap requires them ported rather than rewritten. The inputs are
// carried over verbatim; the expectations are restated in modality units because
// decision #5 deletes the percent Level/Window the originals asserted on.
//
// The legacy percentages are re-derived alongside, which is what makes this a port and
// not a new test that happens to pass: if the real-value answer and the old percent
// answer stop agreeing, the port changed behaviour.
// ---------------------------------------------------------------------------

/** The percent Level/Window the deleted implementation would have produced. */
function legacyLevelAndWindow({ min, max, robustMin, robustMax }) {
    const span = max - min;
    const lowFraction = (robustMin - min) / span;
    const highFraction = (robustMax - min) / span;
    const clamp = (value, low, high) => Math.min(Math.max(value, low), high);
    return {
        level: Math.round(clamp((lowFraction + highFraction) * 50, 0, 100)),
        window: Math.max(1, Math.round(clamp(highFraction - lowFraction, 0, 1) * 100)),
    };
}

test('volumeRange maps robust limits into a real-value window (ported)', () => {
    const range = volumeRange({
        min: -1000,
        max: 3000,
        robustMin: -600,
        robustMax: 2200,
    });

    assert.deepEqual(range, { min: -1000, max: 3000, robustMin: -600, robustMax: 2200 });
    // 800 HU centre over a 2800 HU width -- the same window the old 45/70 percentages
    // named, now stated in the unit a clinician can write down.
    assert.deepEqual(voiFromRange(range), { windowCenter: 800, windowWidth: 2800 });
    assert.deepEqual(legacyLevelAndWindow(range), { level: 45, window: 70 });
});

test('volumeRange safely falls back for invalid or flat metadata (ported)', () => {
    const range = volumeRange({ min: NaN, max: NaN, calMin: 7, calMax: 7 });

    assert.equal(range.min, 7);
    assert.equal(range.max, 8);
    // The old test asserted level 50 / window 100, i.e. "the whole range"; in real
    // values that is the full interval, centred.
    assert.deepEqual(voiFromRange(range), { windowCenter: 7.5, windowWidth: 1 });
    assert.deepEqual(legacyLevelAndWindow(range), { level: 50, window: 100 });
});

// ---------------------------------------------------------------------------
// The rest of the fallback chain
// ---------------------------------------------------------------------------

test('volumeRange prefers the global range and falls back to cal_min/cal_max', () => {
    assert.deepEqual(volumeRange({ min: -500, max: 1500, calMin: 0, calMax: 100 }), {
        min: -500,
        max: 1500,
        robustMin: -500,
        robustMax: 1500,
    });
    assert.deepEqual(volumeRange({ calMin: -200, calMax: 800 }), {
        min: -200,
        max: 800,
        robustMin: -200,
        robustMax: 800,
    });
});

test('volumeRange widens a collapsed robust range back out to the global range', () => {
    // The 99%-air case: every robust sample lands in one bin. Without the widening the
    // viewer opens on a window that clips the anatomy entirely.
    const range = volumeRange({ min: -1000, max: 3000, robustMin: 42, robustMax: 42 });
    assert.deepEqual(range, { min: -1000, max: 3000, robustMin: -1000, robustMax: 3000 });
});

test('volumeRange clamps a robust range that escapes the global range', () => {
    const range = volumeRange({ min: 0, max: 100, robustMin: -50, robustMax: 5000 });
    assert.deepEqual(range, { min: 0, max: 100, robustMin: 0, robustMax: 100 });
});

test('volumeRange never returns an empty interval, whatever it is handed', () => {
    for (const sources of [
        {},
        { min: 5, max: 5 },
        { min: NaN, max: NaN },
        { min: Infinity, max: -Infinity },
        { calMin: NaN, calMax: NaN },
        { min: 10, max: 0 },
    ]) {
        const range = volumeRange(sources);
        assert.ok(range.max > range.min, `empty interval for ${JSON.stringify(sources)}`);
        assert.ok(Number.isFinite(range.min) && Number.isFinite(range.max));
        assert.ok(voiFromRange(range).windowWidth >= MINIMUM_WINDOW_WIDTH);
    }
});

test('voiFromRange floors the width so no renderer divides by zero', () => {
    assert.equal(voiFromRange({ robustMin: 3, robustMax: 3 }).windowWidth, MINIMUM_WINDOW_WIDTH);
    assert.equal(voiFromRange({ robustMin: 3, robustMax: 3 }).windowCenter, 3);
});

// ---------------------------------------------------------------------------
// scalarRange / robustRange
// ---------------------------------------------------------------------------

test('scalarRange ignores non-finite samples instead of propagating them', () => {
    const data = [NaN, -10, 5, Infinity, 20, -Infinity, 0];
    assert.deepEqual(scalarRange(data), { min: -10, max: 20, count: 4, skipped: 3 });
});

test('scalarRange reports NaN for an empty or all-non-finite array', () => {
    assert.deepEqual(scalarRange([]), { min: NaN, max: NaN, count: 0, skipped: 0 });
    assert.deepEqual(scalarRange([NaN, NaN]), { min: NaN, max: NaN, count: 0, skipped: 2 });
});

test('robustRange brackets the bulk of a distribution and excludes the tails', () => {
    // 1000 samples uniform on [0, 999], plus two extreme outliers the 2/98 cuts drop.
    const data = new Float32Array(1002);
    for (let index = 0; index < 1000; index += 1) {
        data[index] = index;
    }
    data[1000] = -100000;
    data[1001] = 100000;

    const { min, max, robustMin, robustMax } = robustRange(data);
    assert.equal(min, -100000);
    assert.equal(max, 100000);
    // Both outliers are excluded, so the robust interval collapses onto the bulk.
    assert.ok(robustMin > -100000, 'low outlier should be cut');
    assert.ok(robustMax < 100000, 'high outlier should be cut');
    assert.ok(robustMin <= 0 && robustMax >= 999, 'the bulk must survive the cut');
});

test('robustRange honours custom percentiles and bin counts', () => {
    const data = new Int16Array(1000);
    for (let index = 0; index < 1000; index += 1) {
        data[index] = index;
    }
    const wide = robustRange(data, { low: 0, high: 1 });
    assert.equal(wide.robustMin, 0);
    assert.ok(Math.abs(wide.robustMax - 999) <= 1000 / HISTOGRAM_BINS + 1e-9);

    const narrow = robustRange(data, { low: 0.4, high: 0.6 });
    assert.ok(narrow.robustMin > 300 && narrow.robustMax < 700, 'a 40-60 cut must be tight');
});

test('robustRange survives a constant volume', () => {
    assert.deepEqual(robustRange(new Float32Array(64).fill(7)), {
        min: 7,
        max: 7,
        robustMin: 7,
        robustMax: 7,
    });
});

test('robustRange survives an empty volume without throwing', () => {
    const { min, robustMin } = robustRange(new Float32Array(0));
    assert.ok(Number.isNaN(min) && Number.isNaN(robustMin));
});

test('the default percentiles clip narrowly, so a study opens darker', () => {
    // Was the 2/98 pair NiiVue used, kept for continuity with the viewer this replaced.
    // Both grids were reported as opening too bright; the upper cut sets the white
    // point, so raising it maps everything below darker.
    assert.deepEqual(DEFAULT_ROBUST_PERCENTILES, { low: 0.005, high: 0.995 });
    assert.ok(DEFAULT_ROBUST_PERCENTILES.high > 0.98, 'darker than the NiiVue window');
});

test('a narrower clip really does widen the window upwards', () => {
    // The property that makes the study darker, rather than the constant restated: a
    // higher upper cut can only move the white point up, never down.
    const data = Float32Array.from({ length: 1000 }, (unused, index) => index);
    const wide = robustRange(data, { low: 0.005, high: 0.995 });
    const narrow = robustRange(data, { low: 0.02, high: 0.98 });
    assert.ok(wide.robustMax >= narrow.robustMax);
    assert.ok(wide.robustMin <= narrow.robustMin);
});

// ---------------------------------------------------------------------------
// Presets (decision #16)
// ---------------------------------------------------------------------------

test('absolute presets exist only where the unit is defined', () => {
    assert.equal(hasAbsolutePresets('ct'), true);
    assert.equal(hasAbsolutePresets('CT'), true);
    // CBCT greyscale is not calibrated Hounsfield and MRI has no absolute unit; both
    // derive their opening window from the data instead (decision #16).
    assert.equal(hasAbsolutePresets('cbct'), false);
    assert.equal(hasAbsolutePresets('mri'), false);
    assert.equal(hasAbsolutePresets('anything-else'), false);
});

test('presetVoi converts a stated interval into a centre and a width', () => {
    // Bone: -450..1050 HU.
    assert.deepEqual(presetVoi('ct', 'bone'), {
        windowCenter: 300,
        windowWidth: 1500,
        label: 'Bone',
    });
    assert.deepEqual(presetVoi('ct', 'brain'), {
        windowCenter: 40,
        windowWidth: 80,
        label: 'Brain',
    });
    assert.equal(presetVoi('ct', 'nonexistent'), null);
    assert.equal(presetVoi('cbct', 'bone'), null);
});

test('every declared preset is a non-empty, correctly ordered interval', () => {
    for (const [modality, presets] of Object.entries(MODALITY_PRESETS)) {
        for (const [name, entry] of Object.entries(presets)) {
            assert.ok(entry.upper > entry.lower, `${modality}.${name} is empty or inverted`);
            assert.ok(entry.label, `${modality}.${name} has no label`);
            assert.deepEqual(presetVoi(modality, name), {
                ...voiFromLimits(entry.lower, entry.upper),
                label: entry.label,
            });
        }
    }
});

// ---------------------------------------------------------------------------
// autoVoi -- where F1 and windowing meet
// ---------------------------------------------------------------------------

/** Raw stored values a CBCT would hold under a `(1, -1024)` encoding. */
function rawCbctData() {
    const data = new Uint16Array(4096);
    for (let index = 0; index < data.length; index += 1) {
        // A broad soft-tissue bulk with a small dense tail.
        data[index] = index < 3900 ? 900 + (index % 200) : 2400 + (index % 100);
    }
    return data;
}

test('autoVoi applies the residual LUT when the loader skipped the rescale', () => {
    // `(1, -1024)` is the branch upstream skips: scalarData is still raw, so the whole
    // window has to move down by 1024 or the CBCT opens on the wrong tissue entirely.
    const header = { scl_slope: 1, scl_inter: -1024, cal_min: 0, cal_max: 0 };
    const result = autoVoi(rawCbctData(), { header });

    assert.deepEqual(result.lut, { rescaleSlope: 1, rescaleIntercept: -1024 });
    assert.equal(result.range.min, 900 - 1024);
    assert.equal(result.range.max, 2499 - 1024);

    // The magnitude of F1, stated as an assertion: a viewer that trusted the loader
    // would open this volume on a window centred exactly 1024 HU too high, and the
    // image would still look like an image.
    const uncorrected = autoVoi(rawCbctData(), { header: { scl_slope: 1, scl_inter: 0 } });
    assert.equal(uncorrected.windowCenter - result.windowCenter, 1024);
    assert.equal(uncorrected.windowWidth, result.windowWidth);
});

test('autoVoi does not re-apply a rescale the loader already performed', () => {
    // `(2, -1024)` is a branch upstream *does* apply, so scalarData already holds
    // modality values and the residual LUT must be identity. Applying the header LUT
    // again here would double the intercept and halve the scale a second time.
    const header = { scl_slope: 2, scl_inter: -1024, cal_min: 0, cal_max: 0 };
    const scaled = Int16Array.from(rawCbctData(), (raw) => raw * 2 - 1024);
    const result = autoVoi(scaled, { header });

    assert.deepEqual(result.lut, { rescaleSlope: 1, rescaleIntercept: 0 });
    assert.equal(result.range.min, 900 * 2 - 1024);
    assert.equal(result.range.max, 2499 * 2 - 1024);
});

test('autoVoi agrees with itself across the two representations of one volume', () => {
    // The same physical volume encoded two ways: raw with a skipped rescale, and
    // pre-scaled with an applied one. The opening window must be the same HU either
    // way -- that equivalence is the whole point of routing through the residual LUT.
    const raw = rawCbctData();
    const skipped = autoVoi(raw, { header: { scl_slope: 1, scl_inter: -1024 } });
    const applied = autoVoi(Float32Array.from(raw, (value) => value - 1024), {
        header: { scl_slope: 3, scl_inter: 7 }, // applied upstream, so residual identity
    });

    assert.ok(
        Math.abs(skipped.windowCenter - applied.windowCenter) < 1e-6,
        `${skipped.windowCenter} vs ${applied.windowCenter}`
    );
    assert.ok(Math.abs(skipped.windowWidth - applied.windowWidth) < 1e-6);
});

test('autoVoi tolerates a header with no usable cal_min/cal_max', () => {
    const result = autoVoi(rawCbctData(), { header: { scl_slope: 0, scl_inter: NaN } });
    assert.deepEqual(result.lut, { rescaleSlope: 1, rescaleIntercept: 0 });
    assert.ok(result.windowWidth > 0);
    assert.ok(Number.isFinite(result.windowCenter));
});
