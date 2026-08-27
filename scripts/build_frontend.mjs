/**
 * Builds the vendored Cornerstone3D bundle. Invoked by scripts/build_frontend.sh.
 *
 * Uses esbuild's JS API rather than the CLI because two things need a plugin:
 * aliasing away itk-wasm's jsdelivr default (F5), and emitting each web worker as its
 * own self-contained bundle at the exact relative depth its `new URL(...,
 * import.meta.url)` call site expects (F4).
 *
 * See docs/cornerstone-roadmap.md, Phase 1.
 */

import { createHash } from 'node:crypto';
import { readFileSync, rmSync, mkdirSync, cpSync, writeFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

import * as esbuild from 'esbuild';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const OUT_ROOT = join(ROOT, 'static', 'vendor', 'cornerstone');
const STATIC_PREFIX = '/static/vendor/cornerstone';

/** esbuild target: the browsers decision #13 implies (WebGL2 + ES modules + top-level await). */
const TARGET = ['chrome120', 'firefox120', 'safari17', 'edge120'];

// ---------------------------------------------------------------------------
// Build identity
// ---------------------------------------------------------------------------

/**
 * The build id is a hash of the *inputs*, not the outputs.
 *
 * An output hash would be circular: `frontend/workers/itk-pipelines-config.js`
 * resolves the pipelines directory from `import.meta.url`, and the app bundle's
 * `publicPath` names the build directory, so the emitted bytes depend on the
 * directory name. Hashing `frontend/**` plus `package-lock.json` is non-circular,
 * deterministic, and changes exactly when a real input changes -- the lockfile
 * covers every vendored byte, since they all come from pinned packages.
 *
 * This file counts as an input too: it decides the output layout, so a change here
 * must invalidate the directory name or a browser could keep serving the old tree
 * from cache.
 */
function computeBuildId() {
    const hash = createHash('sha256');
    const inputs = [
        ...walk(join(ROOT, 'frontend')),
        join(ROOT, 'package-lock.json'),
        fileURLToPath(import.meta.url),
    ];
    for (const file of inputs.sort()) {
        hash.update(relative(ROOT, file).split(sep).join('/'));
        hash.update('\0');
        hash.update(readFileSync(file));
        hash.update('\0');
    }
    return hash.digest('hex').slice(0, 8);
}

/** Directories that are never build inputs and never build outputs. */
const WALK_SKIP = new Set([
    'node_modules',
    // frontend/tests/ is `node --test` material. It does not reach the bundle, so
    // editing a test must not restamp the build directory -- that would rewrite ~20 MB
    // of byte-identical committed output under a new name on every test change.
    'tests',
]);

function walk(dir) {
    const out = [];
    for (const name of readdirSync(dir).sort()) {
        const full = join(dir, name);
        if (statSync(full).isDirectory()) {
            if (WALK_SKIP.has(name)) {
                continue;
            }
            out.push(...walk(full));
        } else {
            out.push(full);
        }
    }
    return out;
}

// ---------------------------------------------------------------------------
// Plugins
// ---------------------------------------------------------------------------

/**
 * Replace itk-wasm's pipelines-base-url module with ours (F5).
 *
 * Matched on the importer so only that one package is affected; the shim's own
 * doc comment explains why aliasing beats calling the setter.
 */
const itkPipelinesBaseUrlPlugin = {
    name: 'ygg-itk-pipelines-base-url',
    setup(build) {
        const shim = join(ROOT, 'frontend', 'vendor-shims', 'itk-pipelines-base-url.js');
        build.onResolve({ filter: /(^|\/)pipelines-base-url(\.js)?$/ }, (args) => {
            if (!args.importer.includes(join('@itk-wasm', 'morphological-contour-interpolation'))) {
                return null;
            }
            return { path: shim };
        });
    },
};

/**
 * Hosts worth reporting when a bundled module still names one.
 *
 * A **warning**, not a failure. The project no longer forbids third-party CDNs -- see
 * `templates/base.html` -- so a CDN in the bundle is a fact to know about, not a broken
 * build. It is still worth printing: the itk-wasm pipelines are aliased to the vendored
 * copies (see {@link itkPipelinesShim}), and if that alias ever stops applying the
 * bundle would silently start fetching a *specific version* of those wasm blobs from
 * elsewhere, which is a compatibility question rather than a policy one.
 */
const REPORTED_CDN_HOSTS = ['cdn.jsdelivr.net', 'unpkg.com', 'cdnjs.cloudflare.com', 'cdnjs.com'];

// ---------------------------------------------------------------------------
// Build units
// ---------------------------------------------------------------------------

const APP_ENTRIES = [
    'volume-grid',
    'photo-stack',
    'mesh-landmarks',
    'panoramic-cpr',
    'video-annotate',
    // Temporary, and the only entry that vendors NiiVue. It exists to clear the Phase 3
    // gate ("the validation harness must be green across the maxillo *and* brain
    // corpora before this merges") and goes when that gate is cleared, together with
    // frontend/imaging/validation/ and the @niivue/niivue devDependency.
    'volume-validation',
];

/**
 * Each worker is emitted at the depth its own call site resolves from. Getting any of
 * these wrong kills the worker at runtime with no build error, which is why
 * scripts/check_bundle_assets.mjs re-derives all of them from the emitted files.
 */
const WORKERS = [
    {
        // @cornerstonejs/tools/utilities/registerComputeWorker.js:10
        //   new Worker(new URL('../workers/computeWorker.js', import.meta.url))
        // resolved from app/ -> <build>/workers/computeWorker.js
        entry: 'node_modules/@cornerstonejs/tools/dist/esm/workers/computeWorker.js',
        outfile: 'workers/computeWorker.js',
    },
    {
        // @cornerstonejs/polymorphic-segmentation/registerPolySegWorker.js:9
        //   new Worker(new URL('./workers/polySegConverters.js', import.meta.url))
        // resolved from app/ -> <build>/app/workers/polySegConverters.js
        entry: 'node_modules/@cornerstonejs/polymorphic-segmentation/dist/esm/workers/polySegConverters.js',
        outfile: 'app/workers/polySegConverters.js',
    },
    {
        // @cornerstonejs/labelmap-interpolation/registerWorker.js:9
        //   new Worker(new URL('./workers/interpolationWorker.js', import.meta.url))
        // resolved from app/ -> <build>/app/workers/interpolationWorker.js
        // Wrapped so the vendored itk pipelines URL is set inside this worker (F5).
        entry: 'frontend/workers/interpolation-worker.js',
        outfile: 'app/workers/interpolationWorker.js',
    },
    {
        // itk-wasm/dist/pipeline/create-web-worker.js:9 -- a *nested* worker the
        // interpolation worker spawns:
        //   new Worker(new URL('./web-workers/itk-wasm-pipeline.worker.js', import.meta.url))
        // resolved from app/workers/ -> <build>/app/workers/web-workers/...
        // Not recorded in the roadmap's F4; found while building Phase 1.
        entry: 'node_modules/itk-wasm/dist/pipeline/web-workers/itk-wasm-pipeline.worker.js',
        outfile: 'app/workers/web-workers/itk-wasm-pipeline.worker.js',
    },
];

/**
 * Re-export stubs that make a shared worker specifier resolve from a second depth.
 *
 * `@cornerstonejs/tools`' registerComputeWorker hardcodes
 * `new URL('../workers/computeWorker.js', import.meta.url)`, and esbuild copies that
 * specifier through untouched. It therefore resolves relative to *whatever emitted
 * file it lands in* -- which is two different places:
 *
 *   app/chunk-*.js           -> <build>/workers/computeWorker.js        (the real one)
 *   app/workers/polySegConverters.js -> <build>/app/workers/computeWorker.js
 *
 * because the polyseg worker imports `utilities` from the tools package index and so
 * carries the string too. Emitting a second 3.4 MB copy to satisfy the second path
 * would be absurd; a module worker can simply import the real one. Nothing calls
 * registerComputeWorker from inside the polyseg worker today, but that is a claim
 * about the current upstream version, not a guarantee -- and the failure mode is a
 * dead worker in a clinical viewer with no build error.
 *
 * scripts/check_bundle_assets.mjs is what found this; F4 does not record it.
 */
const WORKER_STUBS = [
    { at: 'app/workers/computeWorker.js', target: '../../workers/computeWorker.js' },
];

/** Runtime assets fetched by URL rather than imported, so they must be copied. */
const VENDORED_TREES = [
    {
        from: 'node_modules/@itk-wasm/morphological-contour-interpolation/dist/pipelines',
        to: 'itk/pipelines',
    },
    {
        // The 3D orientation marker. `OrientationMarkerTool`'s CUSTOM overlay defaults
        // to fetching this exact file from raw.githubusercontent.com at runtime; it is
        // copied into the build so the entry can resolve it through `import.meta.url`
        // and no third-party host is contacted. Same treatment as F5's jsdelivr default.
        from: 'static/vendor/slicer',
        to: 'orientation',
    },
];

// ---------------------------------------------------------------------------

const common = {
    bundle: true,
    format: 'esm',
    target: TARGET,
    // No sourcemaps: `git diff --exit-code` in CI is the bundle-freshness gate, and
    // sourcemaps embed absolute paths that differ between machines.
    sourcemap: false,
    minify: true,
    legalComments: 'none',
    logLevel: 'warning',
    absWorkingDir: ROOT,
    plugins: [itkPipelinesBaseUrlPlugin],
    loader: { '.wasm': 'file' },
    define: { 'process.env.NODE_ENV': '"production"' },
};

async function main() {
    const build = computeBuildId();
    const outDir = join(OUT_ROOT, build);

    // Remove *every* previous build directory, not just this one: a stale sibling
    // would be committed dead weight and would make `git diff --exit-code` pass while
    // the tree is wrong.
    rmSync(OUT_ROOT, { recursive: true, force: true });
    mkdirSync(outDir, { recursive: true });

    // 1. The five per-surface app bundles, code-split. Chunks stay FLAT in app/:
    //    a nested chunk would move `import.meta.url` and break every worker URL above.
    await esbuild.build({
        ...common,
        entryPoints: APP_ENTRIES.map((name) => join(ROOT, 'frontend', 'entries', `${name}.js`)),
        outdir: join(outDir, 'app'),
        splitting: true,
        chunkNames: 'chunk-[hash]',
        assetNames: 'assets/[name]-[hash]',
        publicPath: `${STATIC_PREFIX}/${build}/app`,
    });

    // 2. Workers, each a self-contained bundle (no splitting -- a worker must not
    //    depend on a sibling chunk laid out for the app graph).
    for (const worker of WORKERS) {
        const outfile = join(outDir, worker.outfile);
        await esbuild.build({
            ...common,
            entryPoints: [join(ROOT, worker.entry)],
            outfile,
            splitting: false,
            assetNames: 'assets/[name]-[hash]',
            publicPath: `${STATIC_PREFIX}/${build}/${dirname(worker.outfile)}`,
        });
    }

    // 3. Depth-shim stubs (see WORKER_STUBS).
    for (const stub of WORKER_STUBS) {
        const path = join(outDir, stub.at);
        mkdirSync(dirname(path), { recursive: true });
        writeFileSync(
            path,
            `// Generated by scripts/build_frontend.sh -- see WORKER_STUBS there.\n` +
                `// Re-exports the single real compute worker so that a '../workers/computeWorker.js'\n` +
                `// specifier also resolves from this directory.\n` +
                `import ${JSON.stringify(stub.target)};\n`
        );
    }

    // 4. Runtime asset trees.
    for (const tree of VENDORED_TREES) {
        cpSync(join(ROOT, tree.from), join(outDir, tree.to), { recursive: true });
    }

    // 5. The manifest Django reads (common/cornerstone_assets.py).
    writeFileSync(
        join(OUT_ROOT, 'manifest.json'),
        JSON.stringify({ build, cornerstone: cornerstoneVersion(), entries: APP_ENTRIES }, null, 2) + '\n'
    );

    reportCdnHosts(outDir);

    console.log(`built static/vendor/cornerstone/${build}/`);
}

function cornerstoneVersion() {
    const pkg = JSON.parse(readFileSync(join(ROOT, 'node_modules/@cornerstonejs/core/package.json'), 'utf8'));
    return pkg.version;
}

function reportCdnHosts(outDir) {
    const found = [];
    for (const file of walk(outDir)) {
        if (!/\.(js|mjs|json|css)$/.test(file)) {
            continue;
        }
        const text = readFileSync(file, 'utf8');
        for (const host of REPORTED_CDN_HOSTS) {
            if (text.includes(host)) {
                found.push(`${relative(ROOT, file)}: ${host}`);
            }
        }
    }
    if (found.length) {
        console.warn(
            'note: the emitted bundle names a third-party CDN. Allowed, but check it is ' +
                'the fetch you meant -- the itk-wasm pipelines are meant to resolve to the ' +
                'vendored copies:\n  ' +
                found.join('\n  ')
        );
    }
}

await main();
