import test from 'node:test';
import assert from 'node:assert/strict';

import {
    DATA_ELEMENT_ID,
    csrfToken,
    pendingCalibrationLine,
    readImageRecords,
    readPhotoData,
} from '../imaging/photos/bootstrap.js';

const OPTIONS = { namespace: 'maxillo', origin: 'https://h' };

// ---------------------------------------------------------------------------
// readPhotoData
// ---------------------------------------------------------------------------

function docWith(text) {
    return {
        getElementById: (id) => (id === DATA_ELEMENT_ID ? { textContent: text } : null),
    };
}

test('the payload is read from the json_script element', () => {
    const data = readPhotoData(
        docWith('{"patientId": 42, "endpoint": "/maxillo/api/x/", "projectNamespace": "maxillo"}')
    );
    assert.equal(data.patientId, 42);
    assert.equal(data.endpoint, '/maxillo/api/x/');
});

test('a missing, malformed or incomplete payload declines rather than throws', () => {
    // The bootstrap has to be able to say "not this page" without taking the tab with
    // it -- the grid's first version returned null from three places in silence, and a
    // blank viewer that reports nothing is indistinguishable from one that failed.
    assert.equal(readPhotoData({ getElementById: () => null }), null);
    assert.equal(readPhotoData(docWith('not json')), null);
    assert.equal(readPhotoData(docWith('{"patientId": 42}')), null, 'no endpoint');
    assert.equal(readPhotoData(docWith('{"endpoint": "/x/"}')), null, 'no patient');
});

// ---------------------------------------------------------------------------
// readImageRecords -- one shape from two endpoints
// ---------------------------------------------------------------------------

test('a list payload becomes one record per image', () => {
    const records = readImageRecords(
        {
            images: [
                { id: 11, index: 1, original_filename: 'a.jpg', image_width: 800, image_height: 600 },
                { id: 12, index: 2, original_filename: 'b.jpg' },
            ],
        },
        OPTIONS
    );
    assert.equal(records.length, 2);
    assert.equal(records[0].fileId, 11);
    assert.equal(records[0].imageId, 'yggweb:https://h/maxillo/api/processing/files/serve/11/a.jpg');
    assert.equal(records[0].width, 800);
});

test('a single-image payload becomes one record too', () => {
    // The teleradiography endpoint returns one image and the intraoral one a list;
    // normalising here keeps that difference out of every consumer.
    const records = readImageRecords(
        { file_id: 7, url: '/maxillo/api/file/7/', pixel_spacing_mm: { x_mm: 0.1, y_mm: 0.1 } },
        OPTIONS
    );
    assert.equal(records.length, 1);
    assert.equal(records[0].fileId, 7);
    assert.deepEqual(records[0].pixelSpacingMm, { x_mm: 0.1, y_mm: 0.1 });
});

test('an uncalibrated image carries null, not a default of one', () => {
    // The null has to survive all the way to the metadata provider, which omits
    // pixelSpacing for it -- that omission is what makes Cornerstone report px.
    const [record] = readImageRecords({ file_id: 7, url: '/x/' }, OPTIONS);
    assert.equal(record.pixelSpacingMm, null);
});

test('a filename with a query string or a slash cannot poison the imageId', () => {
    // The route resolves the file from the id and the filename is decorative -- but a
    // query string in it would make the id unusable, so it is scrubbed rather than
    // trusted. `assertLoaderSafeUrl` refuses a query string outright.
    const [record] = readImageRecords(
        { file_id: 7, url: '/x/', original_filename: 'a b?frame=1/../evil.jpg' },
        OPTIONS
    );
    assert.ok(!record.imageId.includes('?'));
    assert.ok(!record.imageId.includes('..'));
    assert.ok(record.imageId.startsWith('yggweb:https://h/maxillo/api/processing/files/serve/7/'));
});

test('an image with no usable filename still gets one', () => {
    const [record] = readImageRecords({ file_id: 7, url: '/x/' }, OPTIONS);
    assert.match(record.imageId, /serve\/7\/image-7\.jpg$/);
});

test('an entry with no file id is dropped rather than mounted as broken', () => {
    const records = readImageRecords(
        { images: [{ index: 1 }, { id: 0 }, { id: 'nope' }, { id: 12 }] },
        OPTIONS
    );
    assert.deepEqual(
        records.map((record) => record.fileId),
        [12]
    );
});

test('an empty or unrecognised payload yields no records', () => {
    for (const payload of [{}, null, { images: [] }, { count: 0 }]) {
        assert.deepEqual(readImageRecords(payload, OPTIONS), []);
    }
});

// ---------------------------------------------------------------------------
// CSRF
// ---------------------------------------------------------------------------

test('the CSRF token comes from the hidden input, never a cookie', () => {
    // CSRF_USE_SESSIONS = True means there is no csrftoken cookie at all, and
    // CSRF_COOKIE_HTTPONLY would block reading one if there were. The grid's first
    // version read the cookie, found nothing, and every save was a bare 403 with
    // Django's HTML error page instead of a message from the endpoint.
    assert.equal(csrfToken({ querySelector: () => ({ value: 'tok' }) }), 'tok');
    assert.equal(csrfToken({ querySelector: () => null }), '');
    assert.equal(csrfToken(undefined), '');
});

// ---------------------------------------------------------------------------
// The calibration line
// ---------------------------------------------------------------------------

function stackWith(annotations) {
    return { readAnnotations: () => annotations };
}

test('calibration reuses the most recent Length, so no second line tool is needed', () => {
    // The user has already learned to draw a line; a second line-drawing interaction
    // that looked the same and behaved differently would be worse than reusing the first.
    const line = pendingCalibrationLine(
        stackWith([
            { metadata: { toolName: 'Length' }, data: { handles: { points: [[0, 0], [10, 0]] } } },
            { metadata: { toolName: 'Length' }, data: { handles: { points: [[5, 5], [5, 55]] } } },
        ])
    );
    assert.deepEqual(line, { pointA: [5, 5], pointB: [5, 55] });
});

test('an unfinished or absent Length yields no line', () => {
    assert.equal(pendingCalibrationLine(stackWith([])), null);
    assert.equal(
        pendingCalibrationLine(
            stackWith([{ metadata: { toolName: 'Angle' }, data: { handles: { points: [[0, 0], [1, 1], [2, 2]] } } }])
        ),
        null,
        'an angle is not a calibration line'
    );
    assert.equal(
        pendingCalibrationLine(
            stackWith([{ metadata: { toolName: 'Length' }, data: { handles: { points: [[0, 0]] } } }])
        ),
        null,
        'a half-drawn line has one handle'
    );
});

test('only the two ordinates are taken, so a third cannot leak into the request', () => {
    const line = pendingCalibrationLine(
        stackWith([
            { metadata: { toolName: 'Length' }, data: { handles: { points: [[1, 2, 3], [4, 5, 6]] } } },
        ])
    );
    assert.deepEqual(line, { pointA: [1, 2], pointB: [4, 5] });
});
