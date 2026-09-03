# CLAUDE.md

Orientation for AI agents working in this repository. Read
[CONTRIBUTING.md](CONTRIBUTING.md) for the full rules and
[docs/architecture.md](docs/architecture.md) for how the system fits together;
this file is the short version plus the things that waste the most time.

## What this is

A Django 5.2 platform for medical-imaging research (MySQL 8, Redis, Celery,
S3-compatible object storage, Cornerstone3D viewers). Everything runs in Docker.

## App layout

| App | Owns |
|---|---|
| `common/` | Shared infrastructure: `Project`, `ProjectAccess`, `Modality`, `ProcessingStep`, `Job`, `FileRegistry`, object storage, permissions, uploads, export, the runner worker, backups, the domain registry |
| `annotations/` | The durable, versioned annotation record — sets, targets, selectors, revisions, payloads, items |
| `maxillo/` | Dental / maxillofacial imaging (`/maxillo/`) — and, historically, the shared auth, admin and runner-API surfaces |
| `brain/` | Brain-tumour MRI (`/brain/`) |
| `laparoscopy/` | Surgical video (`/laparoscopy/`) |

Each app has its own `README.md` stating what it owns, what it must not own, and
where its boundary with `common/` runs — read the relevant one before adding a
model or a view.

**Import direction is one-way: domain apps → `annotations` → `common`.**
`common/` must never import a domain app or `annotations`; where it needs domain
data it goes through `common/domains.py` or `apps.get_model(...)`.

`maxillo`, `brain` and `laparoscopy` are *domains*, registered in exactly one
place (`common/domains.py`). Behaviour derives from that registry — never add an
`if domain == "…"` branch.

## Running things

Tests:

```bash
docker compose -f docker-compose.dev.yml run --rm --no-deps -T web python manage.py test
```

Without a MySQL server: `python manage.py test --settings=yggdrasil.settings_sqlite_test`.
Convenience only — CI runs against MySQL, and some constraints exist on MySQL but
not SQLite (and vice versa). Anything touching models or constraints must be run
against MySQL.

The dev stack: `./scripts/dev_bootstrap.sh` (idempotent), then
<http://localhost:8000>, login `admin` / `admin`.

## Two things that will bite you

1. **A `requirements.txt` change needs an image rebuild first.** Dependencies are
   baked in at build time, so a stale image fails with an import error that looks
   like a broken branch:

   ```bash
   docker compose -f docker-compose.dev.yml build web
   ```

2. **Touching anything under `frontend/` means rebuilding and committing the
   bundle.**

   ```bash
   npm run build   # -> static/vendor/cornerstone/
   ```

   The emitted bundle is committed so deploys need no Node, and CI runs
   `npm run build` followed by `git diff --exit-code`: **any diff fails the
   build.** The output is byte-reproducible (esbuild pinned exactly, lockfile
   committed, sourcemaps off), so a clean tree after a build is the expected
   state. Frontend tests are `npm test`; `npm run verify` checks every
   worker/wasm URL in the emitted bundle resolves.

## Invariants — do not rediscover these

CONTRIBUTING.md states each of these as a rule, with the reason and the guard.
Read the relevant section before changing code in that area; several of them are
green in every test while being wrong in production.

- [Imaging invariants](CONTRIBUTING.md#imaging-invariants) — RAS reorientation and the
  panoramic; `seg2pano_core.js` must not be vendored; `modalityLutModule` from the raw
  header; NIfTI loader URL rules; `type="module"`; Cornerstone runtime ids are never
  persisted.
- [The `annotations` app](CONTRIBUTING.md#the-annotations-app-layering-is-enforced-by-review)
  — the four-layer rule, revision concurrency, and the schema rules (conditional
  constraints compile to nothing on MySQL; `is_calibrated` for millimetres; identity
  keys cap at 255).
- [Object-storage work is a management command](CONTRIBUTING.md#object-storage-work-is-a-management-command-never-a-migration-or-a-request).
- [The runner HTTP API is frozen](CONTRIBUTING.md#the-runner-http-api-is-frozen) — its
  contract tests are not editable without maintainer sign-off.
- [Database migrations](CONTRIBUTING.md#database-migrations) — the 3.0 baseline is the
  anchor; do not edit or squash it.
- [Third-party CDNs](CONTRIBUTING.md#third-party-cdns) — allowed, but webfonts stay
  self-hosted, pin exact versions, SRI everything pinned.

## House style

- **Do not reformat files wholesale.** The codebase mixes tabs and spaces and
  there is no formatting enforcement; `ruff` runs a deliberately minimal ruleset
  (syntax errors and undefined names only). Widening it is its own PR.
- Volumes are **NIfTI (`.nii.gz`)**. There is no DICOM path.
- Bump `VERSION` and add a `CHANGELOG.md` section in the same PR as a release.
- Development targets `release/3.0`.
