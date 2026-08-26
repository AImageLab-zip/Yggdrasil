import test from 'node:test';
import assert from 'node:assert/strict';

import {
    NIFTI_SCHEME,
    serveFilePath,
    assertServableFilename,
    toAbsoluteUrl,
    assertLoaderSafeUrl,
    volumeUrl,
    niftiVolumeImageId,
    upgradeLegacyServeUrl,
} from '../imaging/ids/imageIds.js';

const ORIGIN = 'https://yggdrasil.ing.unimore.it';

// --- the route shape -------------------------------------------------------

test('the global api namespace has no path prefix', () => {
    assert.equal(
        serveFilePath({ fileId: 123, filename: 'volume.nii.gz' }),
        '/api/processing/files/serve/123/volume.nii.gz'
    );
});

test('a domain namespace prefixes its own path', () => {
    for (const namespace of ['maxillo', 'brain', 'laparoscopy']) {
        assert.equal(
            serveFilePath({ fileId: 7, filename: 'v.nii.gz', namespace }),
            `/${namespace}/api/processing/files/serve/7/v.nii.gz`
        );
    }
});

test('the path never ends in a slash', () => {
    // A trailing slash would put the suffix in the middle, and
    // `new URL(url).pathname.endsWith('.gz')` would go false for a gzipped volume.
    const path = serveFilePath({ fileId: 1, filename: 'a.nii.gz' });
    assert.ok(!path.endsWith('/'));
    assert.ok(path.endsWith('.gz'));
});

test('an unknown namespace is refused', () => {
    assert.throws(() => serveFilePath({ fileId: 1, filename: 'a.nii', namespace: 'nope' }), /namespace/);
});

test('a non-positive-integer fileId is refused', () => {
    for (const fileId of [0, -1, 1.5, 'abc', null, undefined]) {
        assert.throws(() => serveFilePath({ fileId, filename: 'a.nii' }), /fileId/);
    }
});

// --- filename rules --------------------------------------------------------

test('a filename must be a single non-empty segment', () => {
    assert.throws(() => assertServableFilename(''), /required/);
    assert.throws(() => assertServableFilename(undefined), /required/);
    assert.throws(() => assertServableFilename('../secret.nii.gz'), /single path segment/);
    assert.throws(() => assertServableFilename('a\\b.nii'), /single path segment/);
    assert.throws(() => assertServableFilename('a.nii?x=1'), /query or fragment/);
    assert.throws(() => assertServableFilename('a.nii#f'), /query or fragment/);
});

test('an ordinary filename passes', () => {
    assertServableFilename('volume.nii.gz');
    assertServableFilename('panoramic_mip.png');
});

// --- absolutising ----------------------------------------------------------

test('a root-relative path becomes absolute (F3, rule 1)', () => {
    // `new URL(relativePath)` throws with one argument, which is the actual crash
    // inside createNiftiImageIdsAndCacheMetadata.
    assert.equal(
        toAbsoluteUrl('/maxillo/api/processing/files/serve/1/v.nii.gz', { origin: ORIGIN }),
        `${ORIGIN}/maxillo/api/processing/files/serve/1/v.nii.gz`
    );
});

test('an already-absolute URL is preserved', () => {
    const url = `${ORIGIN}/api/processing/files/serve/1/v.nii.gz`;
    assert.equal(toAbsoluteUrl(url, { origin: 'https://elsewhere.invalid' }), url);
});

test('the absolute form is parseable by the single-argument URL constructor', () => {
    const url = volumeUrl({ fileId: 1, filename: 'v.nii.gz', origin: ORIGIN });
    assert.doesNotThrow(() => new URL(url));
    assert.ok(new URL(url).pathname.endsWith('.gz'), 'the .gz must be in the pathname');
});

test('with no origin available at all, it says so instead of guessing', () => {
    assert.throws(() => toAbsoluteUrl('/a/b', {}), /origin/);
});

// --- the ?frame= collision (rule 3) ---------------------------------------

test('a URL carrying a query string is refused, with the reason', () => {
    // The loader does `nifti:${url}?frame=${i}` with a literal '?', so a second query
    // marker makes `frame` part of the previous value and every slice becomes frame 0.
    assert.throws(
        () => assertLoaderSafeUrl(`${ORIGIN}/api/processing/files/serve/1/v.nii.gz?file_key=segmentation_nifti`),
        /frame=N/
    );
});

test('a fragment is refused', () => {
    assert.throws(() => assertLoaderSafeUrl(`${ORIGIN}/a/v.nii.gz#x`), /fragment/);
});

test('a clean URL passes through unchanged', () => {
    const url = `${ORIGIN}/api/processing/files/serve/1/v.nii.gz`;
    assert.equal(assertLoaderSafeUrl(url), url);
});

// --- imageId ---------------------------------------------------------------

test('the imageId is the scheme plus an absolute, query-free URL', () => {
    const imageId = niftiVolumeImageId({
        fileId: 42,
        filename: 'cbct.nii.gz',
        namespace: 'maxillo',
        origin: ORIGIN,
    });
    assert.equal(
        imageId,
        `${NIFTI_SCHEME}:${ORIGIN}/maxillo/api/processing/files/serve/42/cbct.nii.gz`
    );
    // Exactly one '?' may ever appear, and the loader is the one that adds it.
    assert.equal(imageId.split('?').length, 1);
});

test('the imageId carries no Yggdrasil identity beyond the addressable URL', () => {
    // Governing rule: Cornerstone runtime ids are session-scoped and never persisted.
    const imageId = niftiVolumeImageId({ fileId: 42, filename: 'c.nii.gz', origin: ORIGIN });
    assert.ok(!imageId.includes('annotationUID'));
    assert.ok(!imageId.includes('cachedStats'));
});

// --- legacy upgrade --------------------------------------------------------

test('a legacy trailing-slash serve URL is upgraded in place', () => {
    // The shape static/js/viewer_grid.js:1069 builds today.
    assert.equal(
        upgradeLegacyServeUrl('/maxillo/api/processing/files/serve/123/', 'volume.nii.gz'),
        '/maxillo/api/processing/files/serve/123/volume.nii.gz'
    );
});

test('a legacy URL with no trailing slash is also upgraded', () => {
    assert.equal(
        upgradeLegacyServeUrl('/api/processing/files/serve/9', 'v.nii'),
        '/api/processing/files/serve/9/v.nii'
    );
});

test('upgrading refuses to silently drop a file_key', () => {
    // A file_key names a *different* file inside a multi-file bundle. Dropping it
    // would serve the wrong volume with no error at all.
    assert.throws(
        () => upgradeLegacyServeUrl('/api/processing/files/serve/9/?file_key=segmentation_nifti', 'v.nii.gz'),
        /would be lost/
    );
});

test('upgrading a URL that is not a serve URL is refused', () => {
    assert.throws(() => upgradeLegacyServeUrl('/static/js/app.js', 'v.nii'), /Not a file-serve URL/);
});
