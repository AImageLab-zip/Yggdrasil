import test from 'node:test';
import assert from 'node:assert/strict';

import {
    PHOTO_COORDINATE_SYSTEM,
    buildStackSaveRequest,
    groupAnnotationsByFile,
    imageDescriptor,
    restorablesByImageId,
} from '../imaging/photos/photoMeasurements.js';
import { MAX_ANNOTATIONS } from '../imaging/annotations/protocol.js';

// Stands in for Cornerstone's `worldToImageCoords`: this surface's metadata provider uses
// identity cosines, so world (x, y, z) maps to image (x, y) and z is dropped. A real
// annotation's handles are world-space and three-ordinate, which is the whole reason the
// conversion exists -- the first version sent them straight through and the server
// refused every save.
const toImage = (imageId, point) => [point[0], point[1]];
const toWorld = (imageId, point) => [point[0], point[1], 0];

const IMAGES = [
    { fileId: 11, imageId: 'yggweb:https://h/a.jpg', width: 800, height: 600, pixelSpacingMm: null },
    { fileId: 12, imageId: 'yggweb:https://h/b.jpg', width: 800, height: 600, pixelSpacingMm: null },
];

function annotation(imageId, tool = 'Length', points = [[0, 0, 0], [3, 4, 0]]) {
    return {
        annotationUID: 'runtime-only',
        metadata: { toolName: tool, referencedImageId: imageId },
        data: { handles: { points }, cachedStats: { 'x': { length: 999 } } },
    };
}

// ---------------------------------------------------------------------------
// Grouping
// ---------------------------------------------------------------------------

test('annotations are grouped by the image they were drawn on', () => {
    const grouped = groupAnnotationsByFile(
        [annotation(IMAGES[0].imageId), annotation(IMAGES[1].imageId), annotation(IMAGES[0].imageId)],
        new Map(IMAGES.map((image) => [image.imageId, image.fileId]))
    );
    assert.equal(grouped.get(11).length, 2);
    assert.equal(grouped.get(12).length, 1);
});

test('an annotation on an image not in the stack is dropped, not reassigned', () => {
    // That happens when a stack is rebuilt after an image edit, and guessing would move
    // somebody's measurement onto a different photograph.
    const grouped = groupAnnotationsByFile(
        [annotation('yggweb:https://h/gone.jpg')],
        new Map(IMAGES.map((image) => [image.imageId, image.fileId]))
    );
    assert.equal(grouped.size, 0);
});

test('a tool that is not a measurement is filtered out', () => {
    // `getAllAnnotations()` returns everything Cornerstone holds, including state tools
    // keep for themselves -- Crosshairs stores an annotation with no `points` array at
    // all, which made the server refuse the whole save.
    const grouped = groupAnnotationsByFile(
        [annotation(IMAGES[0].imageId, 'Crosshairs'), annotation(IMAGES[0].imageId)],
        new Map(IMAGES.map((image) => [image.imageId, image.fileId]))
    );
    assert.equal(grouped.get(11).length, 1);
});

// ---------------------------------------------------------------------------
// The request
// ---------------------------------------------------------------------------

test('every image gets a group, including the empty ones', () => {
    // The whole point: an image with no entry would be *carried forward* by the server,
    // so clearing one image's measurements would silently restore them. An empty group
    // is how a deletion is expressed.
    const body = buildStackSaveRequest({
        images: IMAGES,
        annotations: [annotation(IMAGES[0].imageId)],
        expectedRevision: 3,
        toImage,
    });
    assert.equal(body.images.length, 2);
    assert.equal(body.images[0].annotations.length, 1);
    assert.deepEqual(body.images[1].annotations, []);
});

test('the frame is image_pixel and the revision travels', () => {
    const body = buildStackSaveRequest({ images: IMAGES, annotations: [], expectedRevision: 0, toImage });
    assert.equal(body.coordinateSystem, PHOTO_COORDINATE_SYSTEM);
    assert.equal(body.coordinateSystem, 'image_pixel');
    assert.equal(body.expectedRevision, 0);
});

test('no measurement value is sent, only geometry', () => {
    // The server recomputes every number from the handles, so a length sent from here
    // would be ignored at best and believed at worst.
    const body = buildStackSaveRequest({
        images: IMAGES,
        annotations: [annotation(IMAGES[0].imageId)],
        expectedRevision: 1,
        toImage,
    });
    const text = JSON.stringify(body);
    assert.ok(!text.includes('"length"') || text.includes('cachedStats'), 'no bare length key');
    // The client passes annotations verbatim; the server strips runtime identifiers. What
    // matters is that nothing here *adds* a computed value.
    for (const image of body.images) {
        for (const entry of image.annotations) {
            assert.ok(!('value' in entry), 'the client must not compute a measurement');
            assert.ok(!('measurements' in entry));
        }
    }
});

test('the descriptor records the shape and who anchored it', () => {
    const descriptor = imageDescriptor(IMAGES[0]);
    assert.deepEqual(descriptor.shape, [800, 600]);
    assert.equal(descriptor.pixel_spacing_mm, null);
    assert.equal(descriptor.recorded_by, 'photo-stack');
});

test('a calibrated image records its spacing in the descriptor', () => {
    const descriptor = imageDescriptor({ ...IMAGES[0], pixelSpacingMm: { x_mm: 0.1, y_mm: 0.2 } });
    assert.deepEqual(descriptor.pixel_spacing_mm, [0.1, 0.2]);
});

test('the cap counts the whole save, matching the server', () => {
    // The guard is against a client resending its buffer, and a loop that did so would
    // spread it over the groups.
    const half = Array.from({ length: MAX_ANNOTATIONS / 2 + 1 }, () => annotation(IMAGES[0].imageId));
    const other = Array.from({ length: MAX_ANNOTATIONS / 2 + 1 }, () => annotation(IMAGES[1].imageId));
    assert.throws(
        () => buildStackSaveRequest({ images: IMAGES, annotations: [...half, ...other], expectedRevision: 0, toImage }),
        new RegExp(`exceeds the ${MAX_ANNOTATIONS}`)
    );
});

test('expectedRevision is required rather than defaulted to zero', () => {
    // Guessing means the second editor on a study loses a 409 they could have avoided.
    for (const expectedRevision of [undefined, null, '0', -1, 1.5]) {
        assert.throws(() =>
            buildStackSaveRequest({ images: IMAGES, annotations: [], expectedRevision, toImage })
        );
    }
});

test('a save with no images is refused', () => {
    assert.throws(() => buildStackSaveRequest({ images: [], annotations: [], expectedRevision: 0 }));
    assert.throws(() => buildStackSaveRequest({ annotations: [], expectedRevision: 0 }));
});

// ---------------------------------------------------------------------------
// Restore
// ---------------------------------------------------------------------------

test('a file the server did not mention restores as empty, not as absent', () => {
    // So the caller restores "nothing" explicitly instead of leaving whatever happened to
    // be on screen from a previous study.
    const restorable = restorablesByImageId(
        [{ fileId: 11, annotations: [storedAnnotation()] }],
        new Map(IMAGES.map((image) => [image.fileId, image.imageId])),
        toWorld
    );
    assert.equal(restorable.get(IMAGES[0].imageId).length, 1);
    assert.deepEqual(restorable.get(IMAGES[1].imageId), []);
});

test('a missing or malformed state restores everything as empty', () => {
    const fileIdToImageId = new Map(IMAGES.map((image) => [image.fileId, image.imageId]));
    for (const state of [undefined, null, [], [{ fileId: 11 }], [{ fileId: 11, annotations: 'nope' }]]) {
        const restorable = restorablesByImageId(state, fileIdToImageId, toWorld);
        assert.equal(restorable.size, 2);
        for (const entries of restorable.values()) {
            assert.deepEqual(entries, []);
        }
    }
});


/** A stored annotation: two-ordinate handles, as the server keeps them. */
function storedAnnotation(points = [[10, 20], [30, 40]]) {
    return {
        metadata: { toolName: 'Length' },
        data: { handles: { points } },
    };
}

// ---------------------------------------------------------------------------
// The conversion -- the bug that made every save fail
// ---------------------------------------------------------------------------

test('handles go out as two ordinates, not the three Cornerstone reports', () => {
    // A StackViewport is 2D on screen and not in its data: every handle is world-space and
    // three-ordinate, for a photograph as for a volume. The first version declared
    // `image_pixel` and sent them untouched, so the server -- correctly refusing a
    // three-ordinate handle in a planar frame -- rejected every single save.
    const body = buildStackSaveRequest({
        images: IMAGES,
        annotations: [annotation(IMAGES[0].imageId, 'Length', [[1, 2, 3], [4, 5, 6]])],
        expectedRevision: 0,
        toImage,
    });
    assert.deepEqual(body.images[0].annotations[0].data.handles.points, [[1, 2], [4, 5]]);
});

test('the conversion is required, not optional', () => {
    // Forgetting it is the exact bug; an optional parameter would let it happen again and
    // fail on the server instead of here.
    assert.throws(
        () => buildStackSaveRequest({ images: IMAGES, annotations: [], expectedRevision: 0 }),
        /toImage is required/
    );
});

test('each annotation converts against the image it was drawn on', () => {
    // Not against the image on screen: a per-image spacing means converting with the wrong
    // image scales the coordinates, and the result looks like a plausible measurement.
    const seen = [];
    buildStackSaveRequest({
        images: IMAGES,
        annotations: [annotation(IMAGES[1].imageId)],
        expectedRevision: 0,
        toImage: (imageId, point) => {
            seen.push(imageId);
            return [point[0], point[1]];
        },
    });
    assert.deepEqual(seen, [IMAGES[1].imageId, IMAGES[1].imageId]);
});

test('restoring converts stored pixels back to world space', () => {
    const restorable = restorablesByImageId(
        [{ fileId: 11, annotations: [storedAnnotation([[10, 20]])] }],
        new Map([[11, IMAGES[0].imageId]]),
        toWorld
    );
    assert.deepEqual(restorable.get(IMAGES[0].imageId)[0].data.handles.points, [[10, 20, 0]]);
});

test('a stored annotation that is already three-ordinate is passed through', () => {
    // A payload from an older client. Converting a world point as though it were pixels
    // would move it a long way and look plausible.
    const restorable = restorablesByImageId(
        [{ fileId: 11, annotations: [storedAnnotation([[10, 20, 30]])] }],
        new Map([[11, IMAGES[0].imageId]]),
        toWorld
    );
    assert.deepEqual(restorable.get(IMAGES[0].imageId)[0].data.handles.points, [[10, 20, 30]]);
});

test('a stored annotation with no handles is dropped rather than crashing the restore', () => {
    const restorable = restorablesByImageId(
        [{ fileId: 11, annotations: [{ metadata: { toolName: 'Length' }, data: {} }] }],
        new Map([[11, IMAGES[0].imageId]]),
        toWorld
    );
    assert.deepEqual(restorable.get(IMAGES[0].imageId), []);
});
