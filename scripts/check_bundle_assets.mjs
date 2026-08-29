/**
 * `npm run verify` -- assert the committed Cornerstone bundle is internally complete.
 *
 * esbuild does not rewrite `new URL(..., import.meta.url)`: it copies the specifier
 * through verbatim, so whether a worker loads depends entirely on where the *emitting
 * file* ended up. Finding F4 of docs/cornerstone-roadmap.md counts three such call
 * sites at two relative depths; building Phase 1 turned up a fourth (itk-wasm spawns
 * a nested pipeline worker from inside the interpolation worker). Every one of them
 * fails at runtime, in a clinical viewer, with no build error. So they are re-derived
 * here from the emitted bytes and checked against the filesystem.
 *
 * It used to enforce a no-third-party-CDN rule as well. That rule is gone -- a CDN is a
 * fine way to serve a static asset and takes load off this deployment, and
 * `templates/base.html` already loads three of them. What survives of it is a *note*:
 * the itk-wasm pipelines are aliased to the vendored copies at build time, and a CDN
 * host reappearing in the bundle means that alias stopped applying and a pinned wasm
 * version is now being fetched by whatever URL the package happened to ship. Worth
 * printing, not worth failing.
 *
 * Exits non-zero with a list of problems; prints a one-line summary when clean.
 */

import { readFileSync, existsSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve, sep, posix } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const VENDOR_DIR = join(ROOT, 'static', 'vendor', 'cornerstone');
const MANIFEST = join(VENDOR_DIR, 'manifest.json');
const STATIC_PREFIX = '/static/vendor/cornerstone/';

/** Hosts worth mentioning in the summary when an emitted file names one. */
const CDN_HOSTS = ['cdn.jsdelivr.net', 'unpkg.com', 'cdnjs.cloudflare.com', 'cdnjs.com'];

/** `new URL("spec", import.meta.url)` -- both quote styles, minified or not. */
const IMPORT_META_URL = /new URL\(\s*(["'])([^"'\n]+)\1\s*,\s*import\.meta\.url\s*\)/g;

/** Absolute public paths esbuild inlined for file-loader assets and chunk imports. */
const PUBLIC_PATH = /\/static\/vendor\/cornerstone\/[A-Za-z0-9][A-Za-z0-9._/-]*/g;

const problems = [];
/** Not problems: things a reader should know, printed either way. */
const noted = [];
const checked = { files: 0, importMetaUrls: 0, publicPaths: 0, codecWasm: 0 };

/**
 * Bare package specifiers the DICOM decoders pass to `new URL(..., import.meta.url)`,
 * mapped to the wasm file each one is really asking for.
 *
 * These cannot resolve as written -- a bare specifier is not a relative path -- so the
 * build copies the four blobs to `<build>/codec-wasm/` under exactly these names and
 * `frontend/entries/volume-grid.js` initialises the loader with a matching
 * `wasmBasePath`. The names are the strings each decoder hands `resolveWasmUrl`, not
 * ours to choose.
 */
const CODEC_WASM_SPECIFIERS = {
    '@cornerstonejs/codec-charls/decodewasm': 'charlswasm_decode.wasm',
    '@cornerstonejs/codec-libjpeg-turbo-8bit/decodewasm': 'libjpegturbowasm_decode.wasm',
    '@cornerstonejs/codec-openjpeg/decodewasm': 'openjpegwasm_decode.wasm',
    '@cornerstonejs/codec-openjph/wasm': 'openjphjs.wasm',
};

function fail(message) {
    problems.push(message);
}

function walk(dir) {
    const out = [];
    for (const name of readdirSync(dir).sort()) {
        const full = join(dir, name);
        if (statSync(full).isDirectory()) {
            out.push(...walk(full));
        } else {
            out.push(full);
        }
    }
    return out;
}

function rel(file) {
    return relative(ROOT, file).split(sep).join('/');
}

// ---------------------------------------------------------------------------
// 1. Manifest, and exactly one build directory
// ---------------------------------------------------------------------------

if (!existsSync(MANIFEST)) {
    fail(`missing ${rel(MANIFEST)} -- run 'npm run build'`);
    report();
}

const manifest = JSON.parse(readFileSync(MANIFEST, 'utf8'));
const buildDir = join(VENDOR_DIR, manifest.build ?? '');

if (!manifest.build || !existsSync(buildDir)) {
    fail(`manifest names build "${manifest.build}" but ${rel(buildDir)} does not exist`);
    report();
}

// A stale sibling build would be committed dead weight, and would let
// `git diff --exit-code` pass while the tree is wrong.
const buildDirs = readdirSync(VENDOR_DIR).filter((name) =>
    statSync(join(VENDOR_DIR, name)).isDirectory()
);
if (buildDirs.length !== 1 || buildDirs[0] !== manifest.build) {
    fail(`expected exactly one build directory (${manifest.build}), found: ${buildDirs.join(', ')}`);
}

// Every entry the manifest advertises must actually be loadable.
for (const entry of manifest.entries ?? []) {
    const entryFile = join(buildDir, 'app', `${entry}.js`);
    if (!existsSync(entryFile)) {
        fail(`manifest entry "${entry}" has no emitted file at ${rel(entryFile)}`);
    }
}

// ---------------------------------------------------------------------------
// 2. Chunks must stay flat in app/
// ---------------------------------------------------------------------------

// A nested chunk changes `import.meta.url` for whatever worker registration lands in
// it, which silently relocates every worker URL above.
for (const file of walk(buildDir)) {
    const r = rel(file);
    if (!/\/chunk-[^/]+\.js$/.test(r)) {
        continue;
    }
    const expected = `static/vendor/cornerstone/${manifest.build}/app/`;
    if (!r.startsWith(expected) || r.slice(expected.length).includes('/')) {
        fail(`chunk is not flat in app/: ${r} -- this moves import.meta.url (F4)`);
    }
}

// ---------------------------------------------------------------------------
// 3. Resolve every asset reference against its own emitting file
// ---------------------------------------------------------------------------

for (const file of walk(buildDir)) {
    if (!/\.(js|mjs|css)$/.test(file)) {
        continue;
    }
    checked.files += 1;
    const text = readFileSync(file, 'utf8');

    for (const host of CDN_HOSTS) {
        if (text.includes(host)) {
            noted.push(`${rel(file)} names ${host}`);
        }
    }

    for (const match of text.matchAll(IMPORT_META_URL)) {
        const spec = match[2];
        checked.importMetaUrls += 1;
        if (CODEC_WASM_SPECIFIERS[spec]) {
            // A *bare package specifier* inside `new URL`, which esbuild copies
            // through untouched and which can therefore never resolve at runtime.
            // Not suppressed: superseded. The loader is initialised with a
            // `wasmBasePath`, and `shared/wasmBasePath.js::resolveWasmUrl` then
            // resolves each codec by file name and never touches this value. So the
            // check becomes "is the replacement actually there", which is the thing
            // that would really break.
            checked.codecWasm += 1;
            const wasm = join(buildDir, 'codec-wasm', CODEC_WASM_SPECIFIERS[spec]);
            if (!existsSync(wasm)) {
                fail(
                    `${rel(file)}: new URL('${spec}', ...) cannot resolve, and its ` +
                        `wasmBasePath replacement ${rel(wasm)} is missing`
                );
            }
            continue;
        }
        if (/^[a-z][a-z0-9+.-]*:/i.test(spec)) {
            fail(`${rel(file)}: new URL('${spec}', import.meta.url) is an absolute URL, not a bundled asset`);
            continue;
        }
        const target = resolve(dirname(file), spec);
        if (!existsSync(target)) {
            fail(
                `${rel(file)}: new URL('${spec}', import.meta.url) resolves to ` +
                    `${rel(target)}, which does not exist`
            );
        }
    }

    for (const match of text.matchAll(PUBLIC_PATH)) {
        const publicPath = match[0];
        checked.publicPaths += 1;
        const target = join(ROOT, 'static', 'vendor', 'cornerstone', ...publicPath.slice(STATIC_PREFIX.length).split(posix.sep));
        if (!existsSync(target)) {
            fail(`${rel(file)}: references ${publicPath}, which does not exist on disk`);
        }
    }
}

report();

function report() {
    for (const note of noted) {
        console.log(`note: ${note}`);
    }
    if (problems.length) {
        console.error(`bundle verification FAILED (${problems.length} problem(s)):`);
        for (const problem of problems) {
            console.error(`  - ${problem}`);
        }
        process.exit(1);
    }
    console.log(
        `bundle ${manifest.build} ok: ${checked.files} files, ` +
            `${checked.importMetaUrls} import.meta.url refs, ` +
            `${checked.codecWasm} codec wasm refs, ` +
            `${checked.publicPaths} public paths` +
            (noted.length ? `, ${noted.length} CDN reference(s) noted above` : '')
    );
}
