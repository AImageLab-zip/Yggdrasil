# Yggdrasil

A Django platform for medical-imaging research. Clinicians and annotators upload
studies, an external compute cluster processes them, and the results are viewed,
annotated and exported — all under one authorization model and one durable
annotation record.

Three research areas are mounted as their own Django apps:

| Area | Prefix | What it holds |
|---|---|---|
| **Maxillo** | `/maxillo/` | Dental / maxillofacial imaging — CBCT, intraoral scans (IOS), intraoral photos, teleradiography, panoramic |
| **Brain** | `/brain/` | Brain-tumour MRI — T1, T1c, T2, FLAIR and segmentation |
| **Laparoscopy** | `/laparoscopy/` | Surgical video |

Live instance: <https://yggdrasil.ing.unimore.it>

## What it does

- **Upload and cataloguing.** Volumes are `.nii.gz` (NIfTI); images, meshes and
  video use their own modality types. Every uploaded byte lands in S3-compatible
  object storage and is recorded as a `FileRegistry` row — the database holds
  rows, the store holds bytes.
- **Automated processing.** An upload creates `Job` rows, one per enabled
  processing step of its modality. A dedicated runner worker dispatches them to a
  SLURM cluster and reports results back over a frozen HTTP API. See
  [docs/runners.md](docs/runners.md).
- **Imaging viewers.** All imaging renders through **Cornerstone3D**, built from
  `frontend/` into a committed bundle: an orthogonal volume grid with
  measurements and segmentation, a tooth-segmentation surface, an IOS mesh
  viewer, a photo viewer with calibrated measurements, a panoramic
  reconstruction (arch fit → slab → projection) and a frame-accurate video
  editor.
- **Durable annotations.** Landmarks, segmentations, classifications, panoramic
  arches, measurements, video regions and quadrant markers are all stored in one
  versioned model in the `annotations/` app — snapshots with revision numbers,
  never deltas, and never carrying viewer-session identifiers.
- **Voice captioning.** Live Whisper speech-to-text notes, versioned and
  editable, in every area.
- **Export.** Structured, shareable exports of patient data, derived artifacts
  and annotation documents, assembled by `common/export_catalog.py`.
- **Operations.** Nightly database backups with retention, a maintenance /
  read-only / lockdown site mode, presence and activity dashboards, and a health
  endpoint.

## Getting started

Everything runs in Docker.

```bash
cp .env.example .env      # then edit; see docs/setup.md
./scripts/dev_bootstrap.sh
```

That brings up MySQL, Redis and a local single-node Garage, migrates and seeds
the database, and serves the app at <http://localhost:8000>.

Full instructions: [docs/setup.md](docs/setup.md).

## Documentation

- [docs/setup.md](docs/setup.md) — first-time setup: `.env`, `DOCKER_SUFFIX`, Docker networks
- [docs/running.md](docs/running.md) — day-to-day commands: start/stop, logs, migrations, shell access
- [docs/architecture.md](docs/architecture.md) — how the pieces fit: apps, request lifecycle, pipeline, annotation model
- [docs/runners.md](docs/runners.md) — distributed runners and the runner callback API
- [docs/admin-tasks.md](docs/admin-tasks.md) — production operations: superusers, backups, maintenance modes, sweeps
- [docs/new-project-type.md](docs/new-project-type.md) — adding a new project app
- [CONTRIBUTING.md](CONTRIBUTING.md) — Docker quickstart, tests, CI, migrations, and the invariants
- [CLAUDE.md](CLAUDE.md) — orientation for AI coding agents

Each app also carries its own README describing what it owns and where its
boundary with `common/` runs: [common](common/README.md),
[annotations](annotations/README.md), [maxillo](maxillo/README.md),
[brain](brain/README.md), [laparoscopy](laparoscopy/README.md).

Notes:

- Django accepts either `DB_NAME`/`DB_USER`/`DB_PASSWORD` or the `MYSQL_*` variables.
- Object storage is S3-compatible (Garage/MinIO) via `OBJECT_STORAGE_*`.

## Contributing

Contributions are welcome — please open an issue or pull request against the
`release/3.0` branch. [CONTRIBUTING.md](CONTRIBUTING.md) covers the quickstart,
the test suite, CI, release conventions, and the rules that are easy to break
silently.

## Contact

For more information or to request an account: [yggdrasil@unimore.it](mailto:yggdrasil@unimore.it)
