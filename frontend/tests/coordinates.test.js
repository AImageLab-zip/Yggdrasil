import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
    annotationToImagePixels,
    annotationToWorld,
    checkRoundTrip,
} from '../imaging/photos/coordinates.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..', '..');

// A faithful stand-in for Cornerstone's pair, with this surface's identity cosines:
// rowCosines [1,0,0], columnCosines [0,1,0], origin [0,0,0], spacing s.
function converters(spacing = 1) {
    return {
        worldToImage: (imageId, world) => [
            (world[0] + spacing / 2) / spacing,
            (world[1] + spacing / 2) / spacing,
        ],
        imageToWorld: (imageId, image) => [
            spacing * (image[0] - 0.5),
            spacing * (image[1] - 0.5),
            0,
        ],
    };
}

function annotation(points) {
    return {
        annotationUID: 'x',
        metadata: { toolName: 'Length' },
        data: { handles: { points, textBox: { hasMoved: false } }, label: '' },
    };
}

test('a world-space annotation becomes two-ordinate image pixels', () => {
    // The bug this module exists for: a StackViewport reports three ordinates even for a
    // photograph, and a save declaring `image_pixel` that sent them untouched was refused
    // in full by the server.
    const { worldToImage } = converters();
    const converted = annotationToImagePixels(
        annotation([[9.5, 19.5, 0], [29.5, 39.5, 0]]),
        'id',
        worldToImage
    );
    assert.deepEqual(converted.data.handles.points, [[10, 20], [30, 40]]);
});

test('the conversion is a copy, leaving the viewer state alone', () => {
    // The annotations came out of Cornerstone's own store; mutating them would move the
    // handles on screen as a side effect of saving.
    const original = annotation([[9.5, 19.5, 0]]);
    const converted = annotationToImagePixels(original, 'id', converters().worldToImage);
    assert.deepEqual(original.data.handles.points, [[9.5, 19.5, 0]]);
    assert.notEqual(converted.data.handles, original.data.handles);
});

test('everything except the handles survives the conversion', () => {
    const converted = annotationToImagePixels(annotation([[9.5, 19.5, 0]]), 'id', converters().worldToImage);
    assert.equal(converted.metadata.toolName, 'Length');
    assert.equal(converted.data.handles.textBox.hasMoved, false);
    assert.equal(converted.data.label, '');
});

test('a round trip through both converters is lossless', () => {
    for (const spacing of [1, 0.1, 2.5]) {
        const { worldToImage, imageToWorld } = converters(spacing);
        const trip = checkRoundTrip('id', { worldToImage, imageToWorld, probe: [10, 20] });
        assert.ok(trip.ok, `spacing ${spacing} deviated by ${trip.deviation}`);
    }
});

test('checkRoundTrip reports the deviation when the pair does not close', () => {
    // Not checking arithmetic -- the two are inverses by construction. It checks that the
    // metadata provider gave them a plane module they can both work from: a missing
    // imagePositionPatient or non-orthonormal cosines produce a mapping that is silently
    // wrong in a way no single measurement would reveal.
    const trip = checkRoundTrip('id', {
        worldToImage: () => [11, 20],
        imageToWorld: () => [0, 0, 0],
        probe: [10, 20],
    });
    assert.equal(trip.ok, false);
    assert.equal(trip.deviation, 1);
});

test('restoring converts stored pixels back to world space', () => {
    const { imageToWorld } = converters();
    const restored = annotationToWorld(annotation([[10, 20]]), 'id', imageToWorld);
    assert.deepEqual(restored.data.handles.points, [[9.5, 19.5, 0]]);
});

test('an already three-ordinate stored handle is passed through, not re-converted', () => {
    // A payload written by an older client. Converting a world point as though it were
    // pixels would move it a long way and look entirely plausible.
    const restored = annotationToWorld(annotation([[1, 2, 3]]), 'id', converters().imageToWorld);
    assert.deepEqual(restored.data.handles.points, [[1, 2, 3]]);
});

test('an annotation with no handles is refused rather than half-converted', () => {
    assert.throws(() => annotationToImagePixels({ data: {} }, 'id', () => []), /without handles/);
    assert.throws(() => annotationToWorld({ data: {} }, 'id', () => []), /without handles/);
});

test('the shipped converters still offset by half a spacing', () => {
    // The half-pixel offset is the easiest thing to get wrong in a reimplementation, and
    // the reason this module injects Cornerstone's pair instead of doing the arithmetic.
    // If a version bump removes it, the stand-in above stops modelling reality and this
    // fails rather than the surface drifting half a pixel.
    for (const name of ['worldToImageCoords', 'imageToWorldCoords']) {
        const source = readFileSync(
            join(REPO, 'node_modules', '@cornerstonejs', 'core', 'dist', 'esm', 'utilities', `${name}.js`),
            'utf8'
        );
        assert.match(source, /0\.5|\/ 2/, `${name} must still carry the half-spacing offset`);
        assert.match(source, /imagePlaneModule/, `${name} must still read imagePlaneModule`);
    }
});

test('the shipped worldToImageCoords returns row then column', () => {
    // With this surface's identity cosines that is (x, y), which is what `image_pixel`
    // means everywhere else here and what the legacy tooth polygons are in. Change the
    // cosines in metadataProvider.js and this stops being true, so the two are coupled.
    const source = readFileSync(
        join(REPO, 'node_modules', '@cornerstonejs', 'core', 'dist', 'esm', 'utilities', 'worldToImageCoords.js'),
        'utf8'
    );
    assert.match(source, /rowDistance \/ rowPixelSpacing[\s\S]*columnDistance \/ columnPixelSpacing/);
});
