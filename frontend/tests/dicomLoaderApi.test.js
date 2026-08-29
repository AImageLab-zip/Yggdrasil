/**
 * Pin the parts of `@cornerstonejs/dicom-image-loader` this repository stands on.
 *
 * Same guard as `attenuatedMip.test.js` and `cprMapperApi.test.js`, and for the same
 * reason: every rule in `imaging/ids/dicomImageIds.js` and `imaging/grid/dicomVolume.js`
 * was read out of the shipped package, and each one fails *silently* if the package
 * moves underneath it -- an unregistered metadata document does not throw, it decodes
 * to nothing; a missed transfer syntax renders noise. A version bump should fail the
 * build instead.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));

/**
 * The package's own `exports` map does not publish these internal subpaths, so they
 * are reached by file URL -- exactly as `attenuatedMip.test.js` reaches the vtk.js
 * shader, and for the same reason: reading the *real* shipped file is the whole point.
 */
const LOADER = join(HERE, '..', '..', 'node_modules', '@cornerstonejs', 'dicom-image-loader', 'dist', 'esm');

function source(...parts) {
    return readFileSync(join(LOADER, ...parts), 'utf8');
}

function load(...parts) {
    return import(pathToFileURL(join(LOADER, ...parts)));
}

test('the wadors entry point still exports the two things we import', async () => {
    const wadors = await import('@cornerstonejs/dicom-image-loader/wadors');
    assert.equal(typeof wadors.register, 'function');
    assert.equal(typeof wadors.metaDataManager, 'object');
    // `prepareDicomSeries` calls exactly this.
    assert.equal(typeof wadors.metaDataManager.add, 'function');
    assert.equal(typeof wadors.metaDataManager.get, 'function');
});

test('imageIdToURI still cuts at the first colon', () => {
    // If this ever became a scheme-aware parse, an https URL would survive either way
    // -- but a `wadors:` prefix with anything before it would not, and the failure is
    // a 404 on a URL nobody can explain.
    const text = source('imageLoader', 'imageIdToURI.js');
    assert.match(text, /indexOf\(':'\)/);
    assert.match(text, /substring/);
});

test('the frame number is still parsed positionally off /frames/', () => {
    // `dicomImageIds` builds 1-based frame numbers because of this: the manager slices
    // at indexOf('/frames/') + 8 and looks up the sibling id ending '1'.
    assert.match(source('imageLoader', 'wadors', 'metaDataManager.js'), /indexOf\('\/frames\/'\) \+ 8/);
});

test('the transfer syntax still comes from the response Content-Type', async () => {
    // This is why `common/dicom/dicomweb.py` sets `transfer-syntax=` explicitly. With
    // no parameter the loader assumes Implicit VR Little Endian: right for an
    // uncompressed frame, and silent noise for a JPEG Lossless one.
    const { getTransferSyntaxForContentType } = await load('imageLoader', 'wadors', 'loadImage.js');
    assert.equal(typeof getTransferSyntaxForContentType, 'function');
    assert.equal(getTransferSyntaxForContentType(''), '1.2.840.10008.1.2');
    assert.equal(
        getTransferSyntaxForContentType('application/octet-stream; transfer-syntax=1.2.840.10008.1.2.1'),
        '1.2.840.10008.1.2.1'
    );
    assert.equal(
        getTransferSyntaxForContentType('application/octet-stream; transfer-syntax=1.2.840.10008.1.2.4.70'),
        '1.2.840.10008.1.2.4.70'
    );
});

test('a non-multipart body is still taken whole as pixel data', async () => {
    // `common/dicom/views.py` returns a plain octet stream rather than a
    // multipart/related envelope. That is only safe because of this branch.
    const { default: extractMultipart } = await load('imageLoader', 'wadors', 'extractMultipart.js');
    const payload = new Uint8Array([1, 2, 3, 4]);
    const extracted = extractMultipart('application/octet-stream', payload);
    assert.deepEqual(Array.from(extracted.pixelData), [1, 2, 3, 4]);
});

test('preScale is still enabled by default, so voxels arrive in modality units', () => {
    // `dicomSeriesHeader` reports an identity residual LUT on the strength of this.
    // If preScale ever defaulted off, the cached array would be raw and every
    // absolute HU window would be wrong -- the DICOM shape of finding F1.
    assert.match(
        source('imageLoader', 'createImage.js'),
        /options\.preScale\s*=\s*\{[\s\S]{0,200}:\s*true,?\s*\}/
    );
});

test('register still installs the wadors image loader under that exact scheme', () => {
    assert.match(source('imageLoader', 'wadors', 'register.js'), /registerImageLoader\('wadors'/);
});
