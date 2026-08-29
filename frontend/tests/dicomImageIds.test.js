import test from 'node:test';
import assert from 'node:assert/strict';

import {
    TAG,
    WADORS_SCHEME,
    assertUid,
    dicomImageIds,
    firstValue,
    framePath,
    orderInstances,
    seriesMetadataPath,
    toAbsolute,
} from '../imaging/ids/dicomImageIds.js';
import {
    dicomSeriesHeader,
    sliceNormal,
    sliceSpacing,
} from '../imaging/metadata/dicomSeriesHeader.js';
import { describeGeometry } from '../imaging/geometry/orientation.js';
import { residualModalityLut } from '../imaging/metadata/modalityLutModule.js';

const ORIGIN = 'https://ygg.example';
const STUDY = '2.25.111';
const SERIES = '2.25.222';

function instance({ sop = '2.25.333', number = 1, z = 0, frames = 1, spacing = [0.25, 0.5] } = {}) {
    const document = {
        '00080018': { vr: 'UI', Value: [sop] },
        '00080060': { vr: 'CS', Value: ['CT'] },
        '00200013': { vr: 'IS', Value: [number] },
        '00200032': { vr: 'DS', Value: [0, 0, z] },
        '00200037': { vr: 'DS', Value: [1, 0, 0, 0, 1, 0] },
        '00280010': { vr: 'US', Value: [512] },
        '00280011': { vr: 'US', Value: [256] },
        '00280030': { vr: 'DS', Value: spacing },
        '00180050': { vr: 'DS', Value: [99] },
    };
    if (frames > 1) {
        document['00280008'] = { vr: 'IS', Value: [frames] };
    }
    return document;
}

// --- ids -------------------------------------------------------------------------

test('an imageId is the wadors scheme followed by an absolute frame URL', () => {
    const [entry] = dicomImageIds({
        studyUid: STUDY, seriesUid: SERIES, instances: [instance()], origin: ORIGIN,
    });
    assert.equal(
        entry.imageId,
        `${WADORS_SCHEME}:${ORIGIN}/api/dicomweb/studies/${STUDY}/series/${SERIES}` +
            '/instances/2.25.333/frames/1'
    );
});

test('the scheme prefix survives imageIdToURI, which cuts at the first colon', () => {
    // `imageLoader/imageIdToURI.js` is substring(indexOf(':') + 1). The https colon
    // must therefore never be the first one, or the URI comes back truncated.
    const [entry] = dicomImageIds({
        studyUid: STUDY, seriesUid: SERIES, instances: [instance()], origin: ORIGIN,
    });
    const uri = entry.imageId.substring(entry.imageId.indexOf(':') + 1);
    assert.equal(uri, `${ORIGIN}/api/dicomweb/studies/${STUDY}/series/${SERIES}/instances/2.25.333/frames/1`);
    assert.ok(uri.startsWith('https://'));
});

test('frame numbers are 1-based and a 0 is refused', () => {
    // `wadors/metaDataManager.js` slices at indexOf('/frames/') + 8 and looks up the
    // sibling id ending '1'. A 0-based number resolves to no metadata at all.
    assert.throws(
        () => framePath({ studyUid: STUDY, seriesUid: SERIES, sopUid: '2.25.3', frame: 0 }),
        /1-based/
    );
});

test('the frame URL ends with /frames/<n>, which the loader parses positionally', () => {
    const path = framePath({ studyUid: STUDY, seriesUid: SERIES, sopUid: '2.25.3', frame: 7 });
    const cut = path.indexOf('/frames/') + 8;
    assert.equal(parseInt(path.slice(cut), 10), 7);
});

test('a multi-frame instance contributes one id per frame', () => {
    const entries = dicomImageIds({
        studyUid: STUDY, seriesUid: SERIES, instances: [instance({ frames: 3 })], origin: ORIGIN,
    });
    assert.equal(entries.length, 3);
    assert.deepEqual(entries.map((e) => e.frame), [1, 2, 3]);
});

test('instances stack by InstanceNumber, not by the order they arrived', () => {
    const entries = dicomImageIds({
        studyUid: STUDY,
        seriesUid: SERIES,
        instances: [
            instance({ sop: '2.25.30', number: 3 }),
            instance({ sop: '2.25.10', number: 1 }),
            instance({ sop: '2.25.20', number: 2 }),
        ],
        origin: ORIGIN,
    });
    assert.deepEqual(entries.map((e) => e.sopUid), ['2.25.10', '2.25.20', '2.25.30']);
});

test('each id carries the document it was built from', () => {
    // So a caller registering metadata cannot pair an id with the wrong instance.
    const entries = dicomImageIds({
        studyUid: STUDY, seriesUid: SERIES, instances: [instance({ sop: '2.25.77' })], origin: ORIGIN,
    });
    assert.equal(firstValue(entries[0].instance, TAG.SOP_INSTANCE_UID), '2.25.77');
});

test('a UID that is not digits and dots is refused', () => {
    assert.throws(() => assertUid('../../etc', 'seriesUid'), /DICOM UID/);
    assert.throws(() => assertUid('', 'seriesUid'), /required/);
});

test('an empty series is an error rather than an empty volume', () => {
    assert.throws(() => dicomImageIds({ studyUid: STUDY, seriesUid: SERIES, instances: [], origin: ORIGIN }), /at least one/);
});

test('firstValue reads a zero-length attribute as undefined', () => {
    // The de-identifier emits several Type-2 elements with no Value at all.
    assert.equal(firstValue({ '00080020': { vr: 'DA' } }, '00080020'), undefined);
});

test('the metadata path is the frame path without the instance', () => {
    assert.equal(
        seriesMetadataPath({ studyUid: STUDY, seriesUid: SERIES }),
        `/api/dicomweb/studies/${STUDY}/series/${SERIES}/metadata`
    );
});

test('toAbsolute needs an origin it can actually use', () => {
    assert.equal(toAbsolute('/x', { origin: ORIGIN }), `${ORIGIN}/x`);
});

// --- the series header ------------------------------------------------------------

test('slice spacing is measured between slices, not read from SliceThickness', () => {
    // SliceThickness is how much tissue a slice integrates; on an overlapping or
    // gapped acquisition it is not the distance between centres, and using it
    // stretches the volume along z with nothing on screen to say so. The synthetic
    // instances declare a deliberately absurd thickness of 99.
    const spacing = sliceSpacing([
        instance({ number: 1, z: 0 }),
        instance({ number: 2, z: 0.4 }),
        instance({ number: 3, z: 0.8 }),
    ]);
    assert.ok(Math.abs(spacing - 0.4) < 1e-9, `expected 0.4, got ${spacing}`);
});

test('a single-slice series falls back to a declared spacing', () => {
    assert.equal(sliceSpacing([instance()]), 99);
});

test('the slice normal is the cross product of the direction cosines', () => {
    assert.deepEqual(sliceNormal([1, 0, 0, 0, 1, 0]), [0, 0, 1]);
});

test('PixelSpacing is [row, column] and is not transposed into the affine', () => {
    // Invisible on an isotropic scan and a shear on every other one.
    const header = dicomSeriesHeader([
        instance({ number: 1, z: 0, spacing: [0.5, 0.25] }),
        instance({ number: 2, z: 1, spacing: [0.5, 0.25] }),
    ]);
    // i runs along the row direction, so its spacing is the *column* spacing.
    assert.equal(header.pixDims[1], 0.25);
    assert.equal(header.pixDims[2], 0.5);
});

test('the header declares its orientation, so the F2 warning cannot fire', () => {
    // DICOM always carries ImageOrientationPatient explicitly. F2 is about NIfTI
    // volumes whose orientation the reader fabricates from pixel dimensions.
    const header = dicomSeriesHeader([instance({ number: 1, z: 0 }), instance({ number: 2, z: 1 })]);
    const geometry = describeGeometry(header);
    assert.equal(geometry.declared, true);
    assert.deepEqual(geometry.issues, []);
});

test('a series with no orientation is refused rather than assigned one', () => {
    const document = instance();
    delete document['00200037'];
    assert.throws(() => dicomSeriesHeader([document]), /no ImageOrientationPatient/);
});

test('the residual modality LUT is identity, because preScale already applied it', () => {
    // Read from the shipped loader: createImage defaults preScale.enabled to true and
    // getScalingParameters feeds it RescaleSlope/Intercept, so the cached array is
    // already in modality units. Identity is exactly what that state means here --
    // there is no F1-style ambiguity for DICOM.
    const header = dicomSeriesHeader([instance({ number: 1, z: 0 }), instance({ number: 2, z: 1 })]);
    assert.deepEqual(residualModalityLut(header), { rescaleSlope: 1, rescaleIntercept: 0 });
});

test('the header geometry survives describeGeometry as an axial LPS volume', () => {
    const header = dicomSeriesHeader([
        instance({ number: 1, z: 0 }),
        instance({ number: 2, z: 0.4 }),
    ]);
    const geometry = describeGeometry(header);
    assert.equal(geometry.hasMetadata, true);
    assert.ok(Math.abs(geometry.spacing[2] - 0.4) < 1e-9);
    assert.deepEqual(geometry.dimensions, [256, 512, 2]);
});

test('orderInstances is what both the ids and the header agree on', () => {
    const ordered = orderInstances([instance({ sop: '2.25.20', number: 2 }), instance({ sop: '2.25.10', number: 1 })]);
    assert.deepEqual(ordered.map((d) => firstValue(d, TAG.SOP_INSTANCE_UID)), ['2.25.10', '2.25.20']);
});
