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
