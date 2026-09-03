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

The normal command, with or without the dev stack running:

```bash
docker compose -f docker-compose.dev.yml run --rm --no-deps -T web python manage.py test
```

`--no-deps` keeps it from starting the other services; drop it if you want the suite to
run against a live MySQL and Garage.

**Without a MySQL server at all**, use the SQLite settings module:

```bash
python manage.py test --settings=yggdrasil.settings_sqlite_test
```

`yggdrasil/settings_sqlite_test.py` exists because `yggdrasil/settings.py` hard-wires
MySQL and validates its credentials at import, so a suite that only ever touches a
`test_` database would otherwise need a database server. It imports the real settings
and swaps in an in-memory SQLite database, disables the SSL redirect and uses a fast
password hasher. It is not used by any deployment.

It is a convenience, not the reference environment. **CI runs against MySQL 8**, and it
has to: several rules in this codebase are green on SQLite and absent on MySQL (see
[Schema rules](#schema-rules)). A change to models or constraints must be run against
MySQL before it is trusted.

**After a change to `requirements.txt`, rebuild the image before running tests** — the
dependencies are baked in at build time, and a stale image fails with an import error
that looks like a broken branch rather than a stale container:

```bash
docker compose -f docker-compose.dev.yml build web
```

The frontend has its own suite (`npm test`), described under
[The frontend bundle](#the-frontend-bundle).

## CI

`.github/workflows/ci.yml` runs on self-hosted runners for pushes to
`release/3.0`/`main` and all PRs:

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

- The **root `package.json` must not have a `"type"` field.** The test files in
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

Where the bundle fits in the system as a whole: [docs/architecture.md](docs/architecture.md).

### Third-party CDNs

CDNs are allowed. Today exactly one template uses one:
`templates/common/user_activity_stats.html` loads two pinned Chart.js scripts from
jsDelivr, both with SRI. `templates/base.html` loads **no** CDN scripts at all. Three
rules apply to anything new:

- **Webfonts stay self-hosted.** A font CDN sees every page view of every visitor,
  which is a GDPR question a JavaScript library does not raise. IBM Plex and Font
  Awesome are served from `static/`.
- **Pin what you load.** A version-less CDN URL changes the viewer under you with no
  commit to bisect.
- **SRI what you pin.** Every third-party `<script>` must carry
  `integrity="sha384-…" crossorigin="anonymous"`. These pages render patient data, and
  without it a CDN compromise executes arbitrary JavaScript in a clinical app; with it
  the script simply fails to load. Safe precisely *because* the URLs are exact pins —
  jsDelivr's caveat about SRI applies to dynamically generated files, which these are
  not. Compute one with
  `curl -sfL <url> | openssl dgst -sha384 -binary | openssl base64 -A`.

## Imaging invariants

### `reorient.js` is load-bearing, and the panoramic is why

The panoramic baker consumes **RAS-ordered voxels**. `frontend/imaging/geometry/reorient.js`
does the reorientation and `frontend/imaging/panoramic/volumeSupply.js` is its one
caller.

**The flip must use the *source* axis length, not the output axis's.** The two agree on
a cube and differ on every real CBCT, so a test corpus of cubes will not catch it.
`frontend/tests/reorient.test.js` guards this.

Do not "simplify" the reorientation away. `common/export_catalog.py` ships the baked
PNGs, so a different-but-defensible convention is still a change to an exported clinical
artifact that nothing in the build would notice — every test would stay green while every
panoramic came out transposed.

### `seg2pano_core.js` is not migrated code, and must not become it

`static/js/seg2pano_core.js` and `static/js/worker/seg2pano_worker.js` are the panoramic's
reconstruction: the arch fit, the slab and the projection. The strips they bake must keep
their exact bytes, so the *viewer* was rewritten around them and they were left untouched.

The bundle reaches the core through the `Seg2PanoCore` **global**, deliberately.
**Importing or vendoring a copy is forbidden**: it would create a second implementation of
the arch mathematics, and the two would diverge silently — the drawn curve would stop
being the curve the projection follows, and nothing would say so.

### `modalityLutModule` is derived from the raw NIfTI header

Never take it from the upstream helper. Upstream combines the rescale factors with `&&`
where it needs `||`, so rescale is skipped whenever *either* factor is neutral — the
`scl_slope=1, scl_inter=-1024` encoding is exactly that case, and it is a real CBCT
encoding. Derive slope and intercept from the header
(`frontend/imaging/metadata/modalityLutModule.js`, guarded by
`frontend/tests/modalityLutModule.test.js`).

### NIfTI loader URL rules

The loader calls `new URL(url)` with one argument, so it **throws on a relative path** —
make URLs absolute before handing them over (`frontend/imaging/ids/imageIds.js`). It then
tests `pathname.endsWith('.gz')` to decide whether to decompress, so:

- the `.gz` suffix must be on the **last path segment**, not behind a query string;
- `file_key` must stay a **query parameter**, never a path segment.

### Cornerstone runtime identifiers are session-scoped

`annotationUID`, `imageId`, `volumeId`, `segmentationId` and `cachedStats` exist only for
the lifetime of a page. **They must never be persisted** — not in a payload, not in a
canonical document, not in an identity key. A record carrying one looks durable while not
being.

### Validating a viewer

Two rules worth keeping whatever the harness of the day is:

- **Compare each viewer against the file's own affine, never against another viewer.**
  A pairwise diff reports agreement when both stacks are wrong the same way, which is
  exactly the population whose header declares no orientation.
- **Seed the sampling.** A gate whose samples cannot be reproduced means "green once, on
  voxels nobody can name".

And: a green data-path check is not evidence that a viewer works. It says a voxel lands
where the affine says and holds the value the header says; it says nothing about whether
tools bind or annotations draw.

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
- **`serializers/`** builds the canonical JSON document.

### Revision concurrency

`UniqueConstraint(annotation_set, revision_number)` is the primitive.
**Pass the revision you loaded to `record_revision`.** Do **not** compute the next number
from a `SELECT MAX(...)+1`, which reopens the race the constraint exists to close. A
losing writer gets a conflict and rebases; there is deliberately no automatic merge.

### Schema rules

- **Conditional constraints compile to nothing on MySQL.**
  `UniqueConstraint(condition=...)` produces no partial index and no error on MySQL — it
  is simply absent in production while passing on SQLite. "Exactly one" rules therefore
  use a **nullable slot column** with a plain unique constraint instead
  (`primary_slot`, `canonical_slot`: `1` or `NULL`).
- **A millimetre measurement requires `is_calibrated`, enforced by a `CHECK`.** An
  uncalibrated length is reported in pixels, never dressed up as a physical size.
- **Identity keys cap at 255 characters** (`SourceResource.identity_key` is
  `varchar(255)`; `annotations/identity.py` holds `MAX_IDENTITY_KEY_LENGTH`). Building a
  longer key is an **error**, never a truncation: MySQL in non-strict mode would truncate
  silently and collide two different resources onto one row.

## Object-storage work is a management command, never a migration or a request

Anything that reads bytes out of object storage lives in a management command. Not a
`RunPython`: row counts are unbounded, a migration doing it blocks the deploy and cannot
resume after failing halfway, and object storage is unreachable in CI. Not a request path
either — a stats sweep downloads whole volumes.

`annotations_normalize_coordinates`, `annotations_materialize_landmarks`,
`annotations_crosscheck` and `annotations_compute_roi_stats` all follow the same
shape, and it is worth copying:

- idempotent;
- `--dry-run` and `--limit`;
- **one bad object costs its own rows and not the sweep** — a corpus pass that dies on
  the first unreadable file is a pass nobody can finish;
- **group by resource before downloading.** A volume shared by twenty annotations must be
  fetched once; the naive loop is O(annotations) round trips, which on a real corpus is
  hours rather than minutes.

## The runner HTTP API is frozen

External processing runners speak the claim/complete/fail HTTP API
(`maxillo/api_views/runner.py`, `maxillo/runner_api_service.py`). Its behaviour
is pinned by the contract tests in `maxillo/tests_runner_api.py`. If your
change makes one of those tests fail, you are breaking deployed runners:
**do not adjust the test without explicit maintainer sign-off.**

## Versioning and releases

- `VERSION` (semver) is read into `settings.APP_VERSION` and shown in the footer.
- Bump `VERSION` and add a `CHANGELOG.md` section in the same PR.
- Pushing a tag `vX.Y.Z` (must equal `VERSION`) triggers `.github/workflows/release.yml`,
  which creates a GitHub release with that changelog section as notes.

## Database migrations

**The 3.0 baseline is the anchor.** The live 3.0 schema is the reference state: the
migration history has been collapsed into a fresh baseline, and there is no supported
rollback path to a 1.9 or 2.0 database. Restoring an old dump and migrating forward is
not a scenario this repository supports any more.

Consequently:

- **Migrations after the baseline are normal migrations.** The additive-only rule that
  applied while 1.9 dumps had to restore onto a 2.0 database no longer applies —
  renames, drops and data migrations are all fair game, judged on their own merits.
- **Do not edit or squash the baseline migrations.** They are what a fresh database is
  built from, and production is already past them.
- **Object-storage work still never belongs in a `RunPython`** (see above). That rule is
  independent of the baseline.
- CI's `makemigrations --check` gate ensures model changes always ship with their
  migrations.
- Run anything touching constraints against **MySQL**, not the SQLite test settings.

## Branch conventions

- Development targets `release/3.0`. Open issues and pull requests against it.
- Dependency pins: `requirements.txt` is fully pinned; a pip-tools lockfile is
  possible future work.
