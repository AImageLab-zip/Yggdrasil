/**
 * The four facts the video surface's design rests on, pinned against `node_modules`.
 *
 * This file exists because of a mistake. The first draft of Phase 10 concluded that
 * Cornerstone 5.8.2 could not render a labelmap on a `VideoViewport`, wrote that up as a
 * finding, and built a bespoke frame decoder and a second image loader to work around
 * it. The conclusion was wrong: `VideoViewport` defines both of the methods the tools'
 * stack labelmap plan calls, and `addImages` builds a `CanvasActor` for exactly this
 * purpose. The error came from reading one truncated grep and not re-reading the file.
 *
 * So the pins here are the *positive* facts, in the manner of `attenuatedMip.test.js`:
 * if a Cornerstone bump removes any of them, this build fails and says which one, rather
 * than the surface quietly losing its masks in a browser nobody is watching.
 */

import { readFileSync } from 'node:fs';
import { strict as assert } from 'node:assert';
import test from 'node:test';

const CORE = 'node_modules/@cornerstonejs/core/dist/esm';
const TOOLS = 'node_modules/@cornerstonejs/tools/dist/esm';

const read = (path) => readFileSync(path, 'utf8');

test('the labelmap render mode resolves to "image" for a viewport with getCurrentImageId', () => {
    const resolver = read(
        `${TOOLS}/stateManagement/segmentation/helpers/getViewportLabelmapRenderMode.js`
    );
    assert.match(
        resolver,
        /typeof compatibilityViewport\.getCurrentImageId === 'function'\s*\?\s*'image'\s*:\s*'unsupported'/,
        'The resolver no longer keys on getCurrentImageId; re-derive how a video viewport reaches the labelmap path.'
    );
    assert.match(
        read(`${CORE}/RenderingEngine/VideoViewport.js`),
        /\n\s*getCurrentImageId\(/,
        'VideoViewport lost getCurrentImageId, so its labelmaps now resolve to "unsupported".'
    );
});

test('the stack labelmap plan calls exactly the two viewport methods VideoViewport provides', () => {
    const plan = read(`${TOOLS}/tools/displayTools/Labelmap/syncStackLabelmapActors.js`);
    assert.match(plan, /viewport\.getImageDataMetadata\(/);
    assert.match(plan, /viewport\.addImages\(/);

    const video = read(`${CORE}/RenderingEngine/VideoViewport.js`);
    for (const method of ['getImageDataMetadata', 'addImages']) {
        assert.match(
            video,
            new RegExp(`\\n\\s*${method}\\(`),
            `VideoViewport no longer defines ${method}; the labelmap path would throw on the first brush stroke, in the browser only.`
        );
    }
});

test('VideoViewport.addImages builds a CanvasActor, which is what carries a labelmap', () => {
    const video = read(`${CORE}/RenderingEngine/VideoViewport.js`);
    assert.match(
        video,
        /createActorMapper\(image\)\s*\{\s*return new CanvasActor\(this, image\);/,
        'VideoViewport no longer wraps labelmap images in a CanvasActor.'
    );
});

test('loadVideoStreamMetadata reads the four modules the provider supplies', () => {
    // `imaging/video/metadata.js` answers these and nothing else. A version that starts
    // asking for a fifth would leave the viewport with an undefined field rather than
    // an error, so the failure would surface as a mis-sized actor.
    const utilities = read(`${CORE}/utilities/VideoUtilities.js`);
    for (const module of [
        'MetadataModules.IMAGE_URL',
        'MetadataModules.GENERAL_SERIES',
        'MetadataModules.CINE',
        'MetadataModules.INSTANCE',
        'MetadataModules.IMAGE_PLANE',
    ]) {
        assert.ok(
            utilities.includes(module),
            `VideoUtilities no longer reads ${module}; re-check the metadata provider.`
        );
    }
    assert.match(
        utilities,
        /imageUrlModule\?\.rendered/,
        'The rendered-URL escape hatch is gone; the imageId would have to be a real DICOMweb /frames/ URL.'
    );
});

test('a labelmap layer is matched to the frame on screen through referencedImageIds', () => {
    // The pin behind `editor.js`'s `labelmapImageIds`. Two upstream facts, and the surface
    // is broken -- silently -- if either changes.
    const mapping = read(
        `${TOOLS}/stateManagement/segmentation/labelmapModel/labelmapImageIdMapping.js`
    );
    assert.match(
        mapping,
        /const referencedImageIds = layer\.referencedImageIds \?\? imageIds;/,
        'The resolver no longer reads layer.referencedImageIds, so re-derive what a ' +
            'per-frame labelmap layer has to state.'
    );

    // And the fallback the surface must never rely on: `layer.imageIds[frameIndex]`,
    // indexing a list with one entry per *annotated* frame by a number that counts every
    // frame of the recording. Right on frame 1 by accident, undefined everywhere else.
    const resolver = read(
        `${TOOLS}/stateManagement/segmentation/labelmapModel/labelmapImageReferenceResolver.js`
    );
    assert.match(
        resolver,
        /currentIndex !== -1 && layer\.imageIds\?\.\[currentIndex\]/,
        'The positional fallback moved; check that an unresolved labelmap still cannot ' +
            'silently pick another frame’s mask.'
    );
    assert.match(
        resolver,
        /getLabelmapImageIdsForReferencedImageId\(layer, referenceImageId\)/,
        'Resolution no longer goes through the referencedImageId map.'
    );
});

test('syncStackLabelmapActors removes the labelmap actors it cannot resolve', () => {
    // Why the missing `referencedImageIds` presented as "the stroke disappears and
    // nothing is logged": an unresolved labelmap is not an error here, it is an actor
    // that goes away.
    const plan = read(`${TOOLS}/tools/displayTools/Labelmap/syncStackLabelmapActors.js`);
    assert.match(
        plan,
        /const staleActorEntries = labelmapActorEntries\.filter\(\s*\(actorEntry\) => !derivedImageIdSet\.has\(actorEntry\.referencedId\)\s*\)/,
        'The stale-actor rule changed; re-check what an unresolved labelmap now does.'
    );
});

test('Viewport guards the renderer when adding an actor and not when removing one', () => {
    // The asymmetry behind `Cannot read properties of undefined (reading 'removeActor')`,
    // which fired on every frame change and after every brush stroke. A `VideoViewport`
    // has no VTK renderer -- it draws on a 2D canvas and its labelmaps are `CanvasActor`s
    // -- so `getRenderer()` is undefined, which `addActor` tolerates and `_removeActor`
    // dereferences. Pinned as the *defect* it is: when a Cornerstone bump adds the guard,
    // this test fails and `declareDataRemoval` can go.
    const viewport = read(`${CORE}/RenderingEngine/Viewport.js`);
    assert.match(viewport, /renderer\?\.addActor\(actor\)/, 'addActor lost its guard too.');
    assert.match(
        viewport,
        /const renderer = this\.getRenderer\(\);\s*\n\s*renderer\.removeActor\(actorEntry\.actor\);/,
        '`_removeActor` now guards its renderer; drop `declareDataRemoval` from editor.js.'
    );
});

test('both labelmap removal paths skip that line for a viewport with removeData', () => {
    // The supported way past the defect above, and the reason the shim is one method
    // rather than an override of `removeActors`.
    const helper = read(
        `${TOOLS}/tools/displayTools/Labelmap/removeLabelmapRepresentationData.js`
    );
    assert.match(
        helper,
        /typeof dataViewport\.removeData !== 'function'\)\s*\{\s*return false;/,
        'The `removeData` escape hatch is gone; re-derive how a video viewport drops a labelmap actor.'
    );
    for (const path of [
        `${TOOLS}/tools/displayTools/Labelmap/labelmapRenderPlan/removeLabelmapRepresentationFromViewport.js`,
        `${TOOLS}/tools/displayTools/Labelmap/syncStackLabelmapActors.js`,
    ]) {
        assert.match(
            read(path),
            /if \(removeLabelmapRepresentationData\(viewport, segmentationId, actorEntry\)\) \{\s*return;/,
            `${path} no longer tries removeData before viewport.removeActors.`
        );
    }
});

test('a contour segmentation tool refuses to draw without a Contour segmentation', () => {
    // Why `polygon` is a plain `PlanarFreehandROI` here. This surface stores labelmaps
    // only, so the check below could never pass and every polygon stroke threw --
    // swallowed by `mouseDownActivate`, which is why the stroke was visible while the
    // mouse was down and simply gone on release.
    assert.match(
        read(`${TOOLS}/tools/base/ContourSegmentationBaseTool.js`),
        /if \(!activeSeg\.representationData\.Contour\) \{\s*throw new Error\(`A contour segmentation must be active`\);/,
        'The precondition moved; re-check whether the contour segmentation tool is usable here.'
    );
    assert.match(
        read(`${TOOLS}/tools/annotation/PlanarFreehandROITool.js`),
        /isContourSegmentationTool\(\) \{\s*return false;\s*\}/,
        'PlanarFreehandROITool became a contour segmentation tool, so it now needs one too.'
    );
    // And the accessor the rasteriser converts world points with, which is the one a
    // VideoViewport actually answers.
    assert.match(
        read(`${CORE}/RenderingEngine/VideoViewport.js`),
        /worldToIndex: \(point\) => \{\s*const canvasPoint = this\.worldToCanvas\(point\);/,
        'VideoViewport.getImageData lost worldToIndex; the polygon fill has no way to reach pixels.'
    );
});

test('the mouse-down handler swallows a tool that throws while creating an annotation', () => {
    // Why the polygon failure had no banner and no visible effect beyond a lost stroke.
    assert.match(
        read(`${TOOLS}/eventDispatchers/mouseEventHandlers/mouseDownActivate.js`),
        /console\.warn\('Error adding new annotation, viewport not ready:'/,
        'The swallow moved; a tool that throws on create may now surface differently.'
    );
});
