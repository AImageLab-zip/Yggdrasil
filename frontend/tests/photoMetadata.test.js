import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
    HANDLED_MODULES,
    USER_CALIBRATION,
    calibratedPixelSpacingFor,
    createPhotoMetadataProvider,
    generalSeriesModuleFor,
    imagePixelModuleFor,
    imagePlaneModuleFor,
    photoRecords,
    registerPhotoRegistry,
    releasePhotoRegistry,
    upstreamWouldKeepSpacing,
} from '../imaging/photos/metadataProvider.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..', '..');

const UNCALIBRATED = { width: 800, height: 600, numberOfComponents: 3, pixelSpacingMm: null };
const CALIBRATED = { ...UNCALIBRATED, pixelSpacingMm: { x_mm: 0.1, y_mm: 0.1 } };

// ---------------------------------------------------------------------------
// The omission the whole surface rests on
// ---------------------------------------------------------------------------

test('an uncalibrated image OMITS pixel spacing -- absent, not null, not 1', () => {
    // The roadmap's rule: no pixelSpacing unless it is actually known. The mechanism is
    // an omission, and the three spellings behave differently: `getImagePlaneModule`
    // tests truthiness, so null and 1 take different branches and only one is right.
    const module = imagePlaneModuleFor(UNCALIBRATED);
    assert.ok(!('pixelSpacing' in module));
    assert.ok(!('rowPixelSpacing' in module));
    assert.ok(!('columnPixelSpacing' in module));
});

test('a module with no spacing fails upstream six-way test, so px is reported', () => {
    // A local transcription of `core/utilities/buildMetadata.js getImagePlaneModule`.
    // Duplicated on purpose: the point is to fail when upstream's predicate and this
    // module's output stop agreeing, which a version bump could do silently.
    assert.equal(upstreamWouldKeepSpacing(imagePlaneModuleFor(UNCALIBRATED)), false);
    assert.equal(upstreamWouldKeepSpacing(imagePlaneModuleFor(CALIBRATED)), true);
});

test('the transcribed predicate matches the shipped one', () => {
    const source = readFileSync(
        join(REPO, 'node_modules', '@cornerstonejs', 'core', 'dist', 'esm', 'utilities', 'buildMetadata.js'),
        'utf8'
    );
    for (const key of [
        'columnPixelSpacing',
        'rowPixelSpacing',
        'columnCosines',
        'rowCosines',
        'imagePositionPatient',
        'imageOrientationPatient',
    ]) {
        assert.match(
            source,
            new RegExp(`imagePlaneModule\\.${key}`),
            `${key} must still be part of upstream's check`
        );
    }
    assert.match(source, /usingDefaultValues: true/);
});

test('this module never sets usingDefaultValues itself', () => {
    // `getImagePlaneModule` returns early when the provider's module already carries the
    // flag, short-circuiting the six-way check entirely. Setting it here would decide
    // the answer by accident.
    for (const record of [UNCALIBRATED, CALIBRATED]) {
        assert.ok(!('usingDefaultValues' in imagePlaneModuleFor(record)));
    }
});

test('a calibrated image carries the spacing in BOTH places', () => {
    // `calibrateIfNecessary` computes hasPixelSpacing from
    // `scale > 0 || (!usingDefaultValues && rowPixelSpacing > 0)`, and the
    // calibratedPixelSpacing payload carries no `scale`. So registering the calibration
    // alone leaves the label reading "px User" -- right about provenance, wrong about
    // the unit. The plane module is what makes usingDefaultValues false.
    const plane = imagePlaneModuleFor(CALIBRATED);
    assert.equal(plane.rowPixelSpacing, 0.1);
    assert.equal(plane.columnPixelSpacing, 0.1);
    assert.deepEqual(plane.pixelSpacing, [0.1, 0.1]);

    const calibration = calibratedPixelSpacingFor(CALIBRATED);
    assert.equal(calibration.type, USER_CALIBRATION);
    assert.equal(calibration.rowPixelSpacing, 0.1);
});

test('the User calibration type is the one the shipped enum defines', () => {
    // getCalibratedUnits appends the type to the unit, so this string is what makes the
    // readout say "mm User" rather than "mm undefined".
    const source = readFileSync(
        join(REPO, 'node_modules', '@cornerstonejs', 'metadata', 'dist', 'esm', 'enums', 'CalibrationTypes.js'),
        'utf8'
    );
    assert.match(source, /CalibrationTypes\["USER"\] = "User"/);
    assert.equal(USER_CALIBRATION, 'User');
});

test('an uncalibrated image registers no calibration at all', () => {
    assert.equal(calibratedPixelSpacingFor(UNCALIBRATED), undefined);
});

// ---------------------------------------------------------------------------
// The three modules whose absence is a TypeError
// ---------------------------------------------------------------------------

test('the three modules buildMetadata destructures are never undefined', () => {
    // `buildMetadata` reads imagePixelModule, generalSeriesModule and imagePlaneModule
    // without a guard, so returning undefined for one is a TypeError rather than a
    // fallback to another provider.
    const provider = createPhotoMetadataProvider(new Map([['id', UNCALIBRATED]]));
    for (const module of ['imagePlaneModule', 'imagePixelModule', 'generalSeriesModule']) {
        assert.notEqual(provider(module, 'id'), undefined, module);
    }
});

test('a photograph declares no frame of reference', () => {
    // A Frame of Reference UID is the claim that these coordinates are comparable with
    // any other series carrying it, which for a photograph is false.
    assert.ok(!('frameOfReferenceUID' in imagePlaneModuleFor(CALIBRATED)));
});

test('the series modality is OT, not a real modality a preset could match', () => {
    // Presets are CT-only per decision #16. Naming a real modality here would let a
    // preset table match and put an authoritative-looking window on a photograph.
    assert.equal(generalSeriesModuleFor(UNCALIBRATED).modality, 'OT');
});

test('colour and greyscale get the right sample counts', () => {
    assert.equal(imagePixelModuleFor(UNCALIBRATED).samplesPerPixel, 3);
    assert.equal(imagePixelModuleFor({ ...UNCALIBRATED, numberOfComponents: 1 }).samplesPerPixel, 1);
    assert.equal(imagePixelModuleFor(UNCALIBRATED).bitsAllocated, 8);
});

// ---------------------------------------------------------------------------
// The provider's own manners
// ---------------------------------------------------------------------------

test('an unknown module falls through rather than being answered emptily', () => {
    // An empty object would shadow whichever provider does know, and report
    // "known, and empty" -- which nothing downstream can distinguish from a real answer.
    const provider = createPhotoMetadataProvider(new Map([['id', UNCALIBRATED]]));
    assert.equal(provider('cineModule', 'id'), undefined);
    assert.equal(provider('scalingModule', 'id'), undefined);
});

test('an unknown imageId is not answered for', () => {
    const provider = createPhotoMetadataProvider(new Map([['id', UNCALIBRATED]]));
    for (const module of HANDLED_MODULES) {
        assert.equal(provider(module, 'other'), undefined, module);
    }
});

test('a non-string imageId is not answered for', () => {
    // Cornerstone passes an array for a multi-frame query; a photo stack has no
    // multi-frame images, so nothing here answers one.
    const provider = createPhotoMetadataProvider(new Map([['id', UNCALIBRATED]]));
    assert.equal(provider('imagePlaneModule', ['id']), undefined);
    assert.equal(provider('imagePlaneModule', undefined), undefined);
});

// ---------------------------------------------------------------------------
// One provider, every mounted surface
// ---------------------------------------------------------------------------

test('the default provider answers for every registered surface, not just the first', () => {
    // The reported failure: a maxillo patient mounts teleradiography *and* the intraoral
    // photographs, `metaData.addProvider` is process-wide so it is called once, and the
    // provider used to close over whichever registry mounted first. The second surface's
    // imageIds then had no `imagePlaneModule` -- which Cornerstone's `buildMetadata`
    // destructures with no null check, so it threw and left a black viewport.
    const tele = registerPhotoRegistry(new Map([['yggweb:tele.jpg', UNCALIBRATED]]));
    const intraoral = registerPhotoRegistry(new Map([['yggweb:front.jpg', CALIBRATED]]));
    try {
        const provider = createPhotoMetadataProvider();

        assert.ok(provider('imagePlaneModule', 'yggweb:tele.jpg'));
        assert.ok(provider('imagePlaneModule', 'yggweb:front.jpg'));
        assert.equal(provider('imagePlaneModule', 'yggweb:absent.jpg'), undefined);
    } finally {
        releasePhotoRegistry(tele);
        releasePhotoRegistry(intraoral);
    }
});

test('a registry stays live after registration, so a calibration is seen', () => {
    // The maps are owned by their bootstrap and written to in place; holding a snapshot
    // would leave a freshly calibrated image reporting pixels until a reload.
    const registry = registerPhotoRegistry(new Map([['yggweb:one.jpg', UNCALIBRATED]]));
    try {
        const provider = createPhotoMetadataProvider();
        assert.equal(provider('calibratedPixelSpacing', 'yggweb:one.jpg'), undefined);

        registry.set('yggweb:one.jpg', CALIBRATED);

        assert.ok(provider('calibratedPixelSpacing', 'yggweb:one.jpg'));
    } finally {
        releasePhotoRegistry(registry);
    }
});

test('a released registry stops answering, so a stale visit cannot shadow a new one', () => {
    const registry = registerPhotoRegistry(new Map([['yggweb:one.jpg', CALIBRATED]]));
    assert.ok(photoRecords.get('yggweb:one.jpg'));

    releasePhotoRegistry(registry);

    assert.equal(photoRecords.get('yggweb:one.jpg'), undefined);
});

test('an explicit registry still scopes a provider to it', () => {
    const shared = registerPhotoRegistry(new Map([['yggweb:shared.jpg', UNCALIBRATED]]));
    try {
        const scoped = createPhotoMetadataProvider(new Map([['yggweb:own.jpg', UNCALIBRATED]]));

        assert.ok(scoped('imagePlaneModule', 'yggweb:own.jpg'));
        assert.equal(scoped('imagePlaneModule', 'yggweb:shared.jpg'), undefined);
    } finally {
        releasePhotoRegistry(shared);
    }
});
