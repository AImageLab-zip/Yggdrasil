# Contributing to Yggdrasil

## Quickstart (Docker only)

```bash
git clone <repo> && cd Yggdrasil
./scripts/dev_bootstrap.sh
```

This brings up MySQL, Redis and a local single-node [Garage](https://garagehq.deuxfleurs.fr/)
(the same S3-compatible object storage used in production), migrates and seeds the
database, and serves the app at <http://localhost:8000> (login `admin` / `admin`).

Re-running the script is safe: seeding (`manage.py seed_dev`) and the Garage init are
idempotent. `seed_dev` refuses to run with `DEBUG=False`.

Stop the stack with `docker compose -f docker-compose.dev.yml down`
(add `-v` to also wipe the database and storage volumes).

## Running tests

With the dev stack up:

```bash
docker compose -f docker-compose.dev.yml run --rm web python manage.py test
```

Or without the dev stack, the raw-docker recipe in
`docs/modernization-roadmap.md` (MySQL 8 + Redis 7 containers on a scratch network).

## CI

`.github/workflows/ci.yml` runs on self-hosted runners for pushes to
`release/3.0`/`release/2.0`/`main` and all PRs:

- **lint** — `ruff check .` with a deliberately minimal ruleset
  (`E9`, `F63`, `F7`, `F82`: syntax errors and undefined names only).
  The codebase mixes tabs/spaces; there is **no formatting enforcement**.
  Do not reformat files wholesale — widening the lint ruleset is its own PR.
- **test** — `makemigrations --check --dry-run` (no unmigrated model changes),
  `migrate`, then the full suite against MySQL 8 + Redis 7 services.
- **frontend** — `npm ci`, `npm run build`, then **`git diff --exit-code`**: the
  committed Cornerstone bundle must be byte-identical to a fresh build. Then
  `npm run verify` (every worker/wasm URL in the emitted bundle resolves) and
  `npm test`. See [The frontend bundle](#the-frontend-bundle).

### Self-hosted runner setup

The workflows expect a repo-level runner with labels `self-hosted, linux` and
Docker available (jobs run in `python:3.11-slim` containers with MySQL/Redis
service containers). Register one via GitHub → Settings → Actions → Runners →
"New self-hosted runner", then run it as a service. The `release` workflow
additionally needs `gh` installed on the runner host, and the `frontend` job needs
**npm-registry egress** (`https://registry.npmjs.org`) from the runner.

## The frontend bundle

Imaging is rendered by Cornerstone3D, built with npm + esbuild. Both are **dev-only**:
the emitted bundle is committed under `static/vendor/cornerstone/`, so deploys need no
Node. That is about **reproducibility**, not about avoiding CDNs: the bundle is a
byte-reproducible artefact CI re-derives and compares, and the viewer's web workers and
wasm blobs have to sit at paths the emitting file can reach (see the `import.meta.url`
note below).

Third-party CDNs are fine, and `templates/base.html` uses three. An earlier rule
forbade them; it is gone. Two things it is worth keeping in mind rather than a policy:

- **Webfonts stay self-hosted.** A font CDN sees every page view of every visitor,
  which is a GDPR question a JavaScript library does not raise. IBM Plex and Font
  Awesome are served from `static/`.
- **Pin what you load.** A version-less CDN URL changes the viewer under you with no
  commit to bisect.
- **SRI what you pin.** Every third-party `<script>` carries
  `integrity="sha384-…" crossorigin="anonymous"`. These pages render patient data, and
  without it a CDN compromise executes arbitrary JavaScript in a clinical app; with it
  the script simply fails to load. Safe precisely *because* the URLs are exact pins —
  jsdelivr's caveat about SRI applies to dynamically generated files, which these are
  not. Compute one with
  `curl -sfL <url> | openssl dgst -sha384 -binary | openssl base64 -A`.

```bash
npm ci               # exact versions from package-lock.json; needs registry egress
npm run build        # -> static/vendor/cornerstone/<build>/ + manifest.json
npm run verify       # every worker/wasm URL resolves; CDN references are noted
npm test             # node:test, both the ESM frontend/ and legacy static/js/tests/
```

**If you touch anything under `frontend/`, run `npm run build` and commit the
result.** CI rebuilds and fails on any diff. Output is byte-reproducible: esbuild is
pinned exactly, the lockfile is committed, and sourcemaps are off.

Two constraints that look like style but are not:

- The **root `package.json` must not have a `"type"` field.** The seven test files in
  `static/js/tests/` are CommonJS and stop resolving if the root scope becomes ESM.
  `frontend/package.json` carries `"type": "module"` for the new source instead.
- The bundle is **ESM, and script tags must be `type="module"`.** Three vendored
  packages locate their web workers via `new URL(..., import.meta.url)`, which does not
  exist in esbuild's IIFE output — an IIFE build silently loses every worker. The
  `{% cornerstone_entry %}` tag therefore has no non-module variant.

Templates load a surface with:

```django
{% load cornerstone %}
{% cornerstone_entry 'volume-grid' %}
```

Details and rationale: [docs/cornerstone-roadmap.md](docs/cornerstone-roadmap.md).

## `panoramicSource.js` and `reorient.js` are scaffolding

`cbct_panorex_editor.js` is Phase 7's to rewrite. Until then it reads its **data** out
of `window.ViewerGrid` — three methods and the `viewergridvolumeready` event — and it
expects **RAS-ordered voxels**, because NiiVue reoriented every volume on load.

`frontend/imaging/grid/panoramicSource.js` reproduces that interface and
`frontend/imaging/geometry/reorient.js` does the reorientation, so that Phase 3 could
delete NiiVue without transposing every exported panoramic. Both go with Phase 7.

Do not build on them, and do not "simplify" the reorientation away: the panoramic was
tuned against NiiVue's output, `export_catalog.py` ships the baked PNGs, and a
different-but-defensible convention is still a change to an exported clinical artifact
that nothing in the build would notice.

## The Phase 3 validation harness is temporary

`frontend/imaging/validation/`, `frontend/entries/volume-validation.js`,
`common/imaging_validation.py`, `templates/common/imaging_validation.html` and the
`@niivue/niivue` devDependency exist to clear one gate: the volume grid may not be
replaced until the harness is green across the maxillo *and* brain corpora. That
bundle entry is the only place in the tree that vendors NiiVue.

**All of it is deleted with the viewer replacement.** Do not build on it, and do not
import from `imaging/validation/` outside the harness. A scaffold that outlives its
gate becomes architecture nobody chose.

Run it at `/imaging-validation/` as a staff user. It is staff-only rather than
`@login_required` on purpose — `common/demo.py` logs anonymous visitors in as a real
user, so an authenticated-only page listing raw volume URLs would be public.

Two rules inside it are load-bearing and look like detail:

- **Both viewers are compared against the file's own affine, never against each
  other.** A pairwise diff reports agreement when both stacks are wrong the same way,
  which is exactly the population whose header declares no orientation.
- **Sampling is seeded.** A gate whose samples cannot be reproduced means "green once,
  on voxels nobody can name". If you add a tier, take the seed as an argument.

## The `annotations` app: layering is enforced by review

`annotations/` is the durable annotation model. Four layers, and the boundaries
are the point:

- **`validators/` is pure.** Values in, `ValidationError` out. No database, no
  object storage, no model instances. That is what lets one rule run in a
  service before a write, in a management command sweeping legacy rows, and in
  a test with a literal dict.
- **`adapters/` is pure translation.** A legacy row or an interchange document
  in, descriptor dicts out. It never queries and never saves.
- **`services/` is the only writer.** A view that imports an annotation model
  and calls `.save()` is a review failure. Every write allocates a revision
  number against the unique constraint, refreshes the monotonic
  `ever_annotated` flag, fingerprints the targets and validates the items in
  one transaction; a caller free to skip a step will eventually skip the flag,
  and then a scan with landmarks on it becomes replaceable.
- **`serializers/`** builds the canonical JSON document. No Cornerstone runtime
  identifier may appear in it — `annotationUID`, `imageId`, `volumeId`,
  `segmentationId`, `cachedStats` are session-scoped, and a document carrying
  one looks durable while not being.

Concurrency is `UniqueConstraint(annotation_set, revision_number)`. Pass the
revision you loaded to `record_revision`; do **not** compute the next number
from a `SELECT MAX(...)`, which reopens the race the constraint exists to close.

Two schema rules look like style and are not. Conditional constraints
(`UniqueConstraint(condition=...)`) compile to **nothing** on MySQL — no partial
index, no error — so "exactly one" rules use a nullable slot column instead. And
a millimetre measurement requires `is_calibrated`, enforced by a `CHECK`: an
uncalibrated length is reported in pixels, never dressed up as a physical size.

## Object-storage work is a management command, never a migration or a request

Anything that reads bytes out of Garage lives in `annotations/management/commands/`.
Not a `RunPython`: row counts are unbounded, a migration doing it blocks the deploy
and cannot resume after failing halfway, and object storage is unreachable in CI.
Not a request path either — `annotations_compute_roi_stats` downloads whole volumes.

`annotations_normalize_coordinates`, `annotations_materialize_landmarks`,
`annotations_crosscheck` and `annotations_compute_roi_stats` all follow the same
shape, and it is worth copying: idempotent, `--dry-run`, `--limit`, and **one bad
object costs its own rows and not the sweep**. A corpus pass that dies on the first
unreadable file is a pass nobody can finish.

Group by resource before downloading. A volume shared by twenty annotations must be
fetched once; the naive loop is O(annotations) round trips, which on a real corpus is
hours rather than minutes.

## The runner HTTP API is frozen

External processing runners speak the claim/complete/fail HTTP API
(`maxillo/api_views/runner.py`, `maxillo/runner_api_service.py`). Its behavior
is pinned by the contract tests in `maxillo/tests_runner_api.py`. If your
change makes one of those tests fail, you are breaking deployed runners:
**do not adjust the test without explicit maintainer sign-off.**

## Versioning and releases

- `VERSION` (semver) is read into `settings.APP_VERSION` and shown in the footer.
- Bump `VERSION` and add a `CHANGELOG.md` section in the same PR.
- Pushing a tag `vX.Y.Z` (must equal `VERSION`) triggers `.github/workflows/release.yml`,
  which creates a GitHub release with that changelog section as notes.
- Tag `v1.9.0` marks the last pre-2.0 production state (rollback reference).

## Database migrations — additive only

Production upgrades restore a v1.9 mysqldump onto a fresh VM and run 2.0 on
top of it (`migrate` runs automatically on container start). Therefore:

- **Never edit or squash migrations that exist at tag `v1.9.0`.**
- All new migrations must be additive: no table renames, no destructive
  schema changes, and they must apply cleanly on a database restored from a
  v1.9 dump.
- CI's `makemigrations --check` gate ensures model changes always ship with
  their migrations.

## Branch conventions

- Development targets `release/2.0` (see `docs/modernization-roadmap.md` for
  the phased plan). `main` reflects the currently deployed 1.x state.
- Dependency pins: `requirements.txt` is fully pinned; a pip-tools lockfile is
  possible future work.
