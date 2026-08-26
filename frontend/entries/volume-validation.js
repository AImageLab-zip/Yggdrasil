/**
 * Entry point: the Phase 3 validation harness (roadmap Phase 3, "Validation harness").
 *
 * **This bundle is temporary.** It exists to clear one gate -- "the validation harness
 * must be green across the maxillo *and* brain corpora before this merges" -- and it
 * is the only place in the tree that vendors NiiVue. When Phase 3's viewer replacement
 * lands and the corpora are signed off, this entry, `imaging/validation/`, and the
 * `@niivue/niivue` devDependency all go together. It is a scaffold, and scaffolds that
 * are not removed become architecture.
 *
 * Shape follows `maxillo/views/panoramic_warmup.py`: an admin-gated page that drives
 * real studies through the real code path, rather than a second implementation that
 * would need keeping in sync with the first.
 *
 * What runs, per study:
 *
 *   - the volume is loaded **twice**, once through Cornerstone's NIfTI loader and once
 *     through NiiVue 0.69, from the same URL;
 *   - the raw bytes are fetched a **third** time and parsed with `nifti-reader-js`
 *     directly, which is the reference both legs are measured against;
 *   - Tier 1 (geometry) and Tier 2 (intensity) run over the result.
 *
 * Three fetches of the same volume is deliberate. Reusing one parse would make the
 * reference leg share code with whichever stack produced it, and a shared bug would
 * then be invisible -- which is the one failure mode a validation harness may not have.
 */

import {
    RenderingEngine,
    Enums as coreEnums,
    imageLoader,
    volumeLoader,
    cache,
    setVolumesForViewports,
} from '@cornerstonejs/core';

import {
    cornerstoneNiftiImageLoader,
    createNiftiImageIdsAndCacheMetadata,
} from '@cornerstonejs/nifti-volume-loader';

import { Niivue } from '@niivue/niivue';

import { initImaging } from '../imaging/runtime/init.js';
import { niftiVolumeImageId, volumeUrl } from '../imaging/ids/imageIds.js';
import { describeGeometry } from '../imaging/geometry/orientation.js';
import { autoVoi } from '../imaging/windowing/autoVoi.js';
import { runTier1 } from '../imaging/validation/tier1Geometry.js';
import { runTier2 } from '../imaging/validation/tier2Intensity.js';
import { cachedScalarData, cornerstoneLeg, niivueLeg } from '../imaging/validation/adapters.js';
import { allFixtures } from '../imaging/validation/fixtures.js';
import { formatReport, summarize } from '../imaging/validation/report.js';
import { DEFAULT_SEED } from '../imaging/validation/prng.js';
import { IMAGE_LOADER_SCHEME, volumeIdFor } from '../imaging/grid/layout.js';

export const SURFACE = 'volume-validation';

/** Tier 1 samples fewer voxels here than the default; a browser tab has to stay alive. */
export const BROWSER_SAMPLE_COUNT = 10000;

/**
 * Parse a NIfTI buffer with the vendored reader -- the reference leg.
 *
 * `static/js/nifti-reader.js` is built from `nifti-reader-js@0.6.9`, verified byte for
 * byte against the package in `node_modules` (Phase 3 preflight). It is loaded as a
 * global by `templates/base.html`, so the harness page has it without a second copy.
 */
function readReferenceHeader(buffer) {
    const reader = globalThis.nifti;
    if (!reader) {
        throw new Error(
            'The vendored nifti-reader is not loaded; the harness has no reference leg.'
        );
    }
    const decompressed = reader.isCompressed(buffer) ? reader.decompress(buffer) : buffer;
    const header = reader.readHeader(decompressed);
    return { header, image: reader.readImage(header, decompressed) };
}

/** The typed array a NIfTI datatype code implies, for reading the reference image. */
const RAW_ARRAYS = new Map([
    [2, Uint8Array],
    [4, Int16Array],
    [8, Int32Array],
    [16, Float32Array],
    [64, Float64Array],
    [256, Int8Array],
    [512, Uint16Array],
    [768, Uint32Array],
]);

function rawVoxels(header, image) {
    const Constructor = RAW_ARRAYS.get(header.datatypeCode);
    if (!Constructor) {
        throw new Error(`NIfTI datatypeCode ${header.datatypeCode} is not supported by the harness.`);
    }
    return new Constructor(image);
}

/**
 * Load one volume through Cornerstone and return the volume object.
 *
 * @param {string} url absolute, query-free, extension-carrying.
 * @returns {Promise<object>}
 */
async function loadThroughCornerstone(url) {
    const imageIds = await createNiftiImageIdsAndCacheMetadata({ url });
    if (!imageIds?.length) {
        throw new Error('Cornerstone produced no imageIds for this volume.');
    }
    // NOT `nifti:` -- that is the *image* loader's scheme, and using it here routes
    // volume loading into the image loader. See VOLUME_ID_SCHEME.
    const volumeId = volumeIdFor(url);
    const volume = await volumeLoader.createAndCacheVolume(volumeId, { imageIds });
    await volume.load();
    return volume;
}

/**
 * Load one volume through NiiVue 0.69 and return `{niivue, image}`.
 *
 * The canvas is off-screen and never shown: NiiVue needs a WebGL context to compute
 * `permRAS` and the frac2mm matrices at all, but the harness only reads numbers off it.
 * Tier 3 (appearance) is where NiiVue is actually looked at, and Tier 3 is not a gate.
 *
 * @param {string} url
 * @param {Document} doc
 * @returns {Promise<{niivue: object, image: object, canvas: HTMLCanvasElement}>}
 */
async function loadThroughNiivue(url, doc) {
    const canvas = doc.createElement('canvas');
    canvas.width = 64;
    canvas.height = 64;
    canvas.style.position = 'absolute';
    canvas.style.left = '-10000px';
    doc.body.appendChild(canvas);

    const niivue = new Niivue({ logLevel: 'silent', show3Dcrosshair: false });
    await niivue.attachToCanvas(canvas);
    await niivue.loadVolumes([{ url }]);

    const image = niivue.volumes?.[0];
    if (!image) {
        throw new Error('NiiVue loaded no volume from this URL.');
    }
    return { niivue, image, canvas };
}

/** Tear down one NiiVue instance and its canvas. */
function disposeNiivue(loaded) {
    try {
        loaded?.niivue?.volumes?.splice(0);
        loaded?.canvas?.remove();
    } catch {
        // A failed teardown must not mask the result the run just produced.
    }
}

/**
 * Validate one volume, by URL.
 *
 * Every leg is caught independently. A NiiVue failure must not lose the Cornerstone
 * result: NiiVue is the stack being deleted, and "NiiVue would not load it" is
 * information, not a reason to abandon the study.
 *
 * @param {object} options
 * @param {string} options.study a human-readable name for the report.
 * @param {string} options.url
 * @param {Document} [options.doc]
 * @param {number} [options.seed]
 * @param {number} [options.sampleCount]
 * @returns {Promise<object>} a run, for `report.js`.
 */
export async function validateVolume({
    study,
    url,
    doc = globalThis.document,
    seed = DEFAULT_SEED,
    sampleCount = BROWSER_SAMPLE_COUNT,
}) {
    let niivueLoaded = null;
    let volume = null;

    try {
        // The reference: a third, independent read of the same bytes.
        const response = await fetch(url, { credentials: 'same-origin' });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status} fetching the volume.`);
        }
        const { header, image } = readReferenceHeader(await response.arrayBuffer());
        const raw = rawVoxels(header, image);

        volume = await loadThroughCornerstone(url);
        const legs = [cornerstoneLeg({ volume })];

        const niivueError = await (async () => {
            try {
                niivueLoaded = await loadThroughNiivue(url, doc);
                legs.push(
                    niivueLeg({ niivue: niivueLoaded.niivue, image: niivueLoaded.image, header })
                );
                return null;
            } catch (error) {
                return error.message;
            }
        })();

        const tier1 = runTier1({ header, legs, sampleCount, seed });
        if (niivueError) {
            tier1.warnings = [
                ...(tier1.warnings ?? []),
                `NiiVue could not provide a leg for this study (${niivueError}); ` +
                    'Tier 1 compared Cornerstone against the file affine only.',
            ];
        }

        const tier2 = runTier2({ cached: cachedScalarData(volume), raw, header, seed });

        return {
            study,
            url,
            tier1,
            tier2,
            // Reported, not gated: the opening window a clinician will actually see.
            openingVoi: autoVoi(cachedScalarData(volume), { header }),
            geometry: describeGeometry(header),
        };
    } catch (error) {
        return { study, url, error: error.message };
    } finally {
        disposeNiivue(niivueLoaded);
        if (volume) {
            // A CBCT is hundreds of megabytes; leaving them cached would exhaust the
            // budget three studies into a corpus run.
            try {
                cache.removeVolumeLoadObject(volume.volumeId);
            } catch {
                // Already evicted.
            }
        }
    }
}

/**
 * Validate the synthetic fixtures -- the cases no real corpus contains.
 *
 * Served to the loader as blob URLs so they take the same path a real study does: the
 * same fetch, the same header parse, the same `modalityScaleNifti`. A fixture handed
 * straight to the comparison functions would skip the part being validated.
 *
 * @param {object} [options]
 * @returns {Promise<object[]>}
 */
export async function validateFixtures({ doc = globalThis.document, seed = DEFAULT_SEED } = {}) {
    const runs = [];
    for (const fixture of allFixtures()) {
        const blob = new Blob([fixture.buffer], { type: 'application/octet-stream' });
        const url = URL.createObjectURL(blob);
        try {
            const run = await validateVolume({
                study: `fixture: ${fixture.name}`,
                url,
                doc,
                seed,
                // The fixtures are tiny; compare every voxel rather than a sample.
                sampleCount: 4096,
            });
            run.fixture = {
                expectation: fixture.expectation,
                upstreamSkips: fixture.upstreamSkips,
                expectsWarning: Boolean(fixture.expectsWarning),
            };
            runs.push(run);
        } finally {
            URL.revokeObjectURL(url);
        }
    }
    return runs;
}

/**
 * Run the whole harness: fixtures first, then the studies the page was given.
 *
 * Fixtures first on purpose. If a synthetic case fails, the harness itself is broken
 * and the corpus numbers that follow mean nothing -- better to see that at the top of
 * the report than after two hundred studies.
 *
 * @param {object} options
 * @param {object[]} options.studies `{study, fileId, filename, namespace, bundleKey}`.
 * @param {(progress: object) => void} [options.onProgress]
 * @param {boolean} [options.includeFixtures]
 * @param {number} [options.seed]
 * @returns {Promise<{runs: object[], summary: object, text: string}>}
 */
export async function runValidation({
    studies = [],
    onProgress = () => {},
    includeFixtures = true,
    seed = DEFAULT_SEED,
} = {}) {
    const capabilities = await initImaging();
    // The NIfTI loader is an **image** loader: it serves the per-frame
    // `nifti:<url>?frame=N` ids. Registering it as a *volume* loader routes volume
    // loading into it, where it looks up `imagePlaneModule` for an id that has no
    // per-frame metadata and dies on `const { rows, columns } = imagePlaneModule`.
    // The volume itself is built by the default streaming loader, which any
    // unregistered volume-id scheme falls through to -- hence VOLUME_ID_SCHEME.
    imageLoader.registerImageLoader(IMAGE_LOADER_SCHEME, cornerstoneNiftiImageLoader);

    const runs = [];
    const total = studies.length + (includeFixtures ? allFixtures().length : 0);

    if (includeFixtures) {
        for (const run of await validateFixtures({ seed })) {
            runs.push(run);
            onProgress({ done: runs.length, total, run });
        }
    }

    for (const study of studies) {
        const url = volumeUrl({
            fileId: study.fileId,
            filename: study.filename,
            namespace: study.namespace,
            bundleKey: study.bundleKey,
        });
        const run = await validateVolume({ study: study.study, url, seed });
        runs.push(run);
        onProgress({ done: runs.length, total, run });
    }

    const summary = summarize(runs);
    return { runs, summary, text: formatReport(runs), capabilities };
}

export {
    RenderingEngine,
    coreEnums,
    setVolumesForViewports,
    niftiVolumeImageId,
    summarize,
    formatReport,
};
