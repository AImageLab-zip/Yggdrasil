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
  `npm run verify` (worker/wasm URLs resolve, no CDN host in any emitted file) and
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
Node and make no network request. This is deliberate — `templates/base.html:42-44`
states the policy that no third-party CDN may be contacted at runtime.

```bash
npm ci               # exact versions from package-lock.json; needs registry egress
npm run build        # -> static/vendor/cornerstone/<build>/ + manifest.json
npm run verify       # every worker/wasm URL resolves; no CDN host is named
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
