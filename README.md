# Yggdrasil

A Django platform for medical-imaging research. Clinicians and annotators upload
studies, an external compute cluster processes them, and the results are viewed,
annotated and exported — all under one authorization model and one durable
annotation record.

Five research areas are mounted as their own Django apps:

| Area | Prefix | What it holds |
|---|---|---|
| **Maxillo** | `/maxillo/` | Dental / maxillofacial imaging — CBCT, intraoral scans (IOS), intraoral photos, teleradiography, panoramic |
| **Brain** | `/brain/` | Brain-tumour MRI — T1, T1c, T2, FLAIR and segmentation |
| **Laparoscopy** | `/laparoscopy/` | Surgical video |
| **Urology** | `/urology/` | Multimodal urology — multiparametric prostate MRI, digital pathology Whole Slide Images (WSI), confocal laser endomicroscopy |
| **Cardiology** | `/cardiology/` | ECG review — clinical-grid waveform plots and AF / NSR / Other / NI rhythm classification |

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
  reconstruction (arch fit → slab → projection), a frame-accurate video
  editor, and a digital pathology Whole Slide Image (WSI) multi-resolution
  deep-zoom viewer with synchronized segmentation masks and an in-browser
  WebAssembly (`wasm-vips`) gigapixel pyramidal TIFF converter.
- **Durable annotations.** Landmarks, segmentations, classifications, panoramic
  arches, measurements, video-frame segmentation and quadrant markers are all stored in one
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

## API access for scripts

Everything under `/api/` can be called from a script with a **personal API
token** instead of a browser session. A token acts as its user, with exactly that
user's project permissions. It is not a separate role.

**Getting a token.** A superuser issues it, either in the admin (*Projects &
access → API tokens → Add*) or from the shell:

```bash
python manage.py create_api_token alice --name "nightly upload" --days 90
```

The token (`ygg_…`) is shown **once**. Only a hash is stored, so a lost token
can't be recovered: revoke it in the admin and issue a new one. Tokens can
expire, are refused once revoked or once their user is deactivated, and record
when they were last used.

**Using it.** Send the token as a bearer header. Token requests need no CSRF
token and create no session:

```bash
export YGG=https://yggdrasil.example.org
export TOKEN=ygg_...
curl -H "Authorization: Bearer $TOKEN" $YGG/api/projects/
```

A missing, revoked or expired token gets `401`. After 20 bad tokens from one
address in 15 minutes, that address gets `429` until the window passes. Tokens
work only under `/api/`. The HTML pages still need a login, and the runner API
(`/api/runner/`) keeps its own `RUNNER_API_TOKENS`.

### Projects

| Request | Who | Returns |
|---|---|---|
| `GET /api/projects/[?domain=brain&name=Study]` | any user | the projects you can read |
| `GET /api/projects/<slug>/` | any user | one project. `404` if it doesn't exist **or** you can't read it |
| `POST /api/projects/` | superusers | `201` with the new project. `409` with the existing `slug` if the name (in that domain) or the slug is taken |

A project as returned:

```json
{"id": 7, "slug": "new-study", "name": "New Study", "domain": "brain",
 "description": "", "is_active": true, "modalities": ["t1", "t2"],
 "annotation_methods": [], "role": "admin", "created_at": "2026-10-19T15:47:13+00:00"}
```

Creating one (JSON body). `slug` is optional and defaults to the slugified name.
`modalities` and `annotation_methods` take slugs and must belong to the project's
domain or to every domain:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "New Study", "domain": "brain", "modalities": ["t1", "t2"]}' \
  $YGG/api/projects/
```

**Enable the modalities you intend to upload.** A new project starts with none,
and an upload silently drops files of any modality the project doesn't enable.
The creator gets the `admin` role on the new project.

The usual pattern for an unattended uploader is: look the project up, create it
on `404` (treating `409` as "already there"), then upload.

### Uploading (maxillo projects)

| Request | Returns |
|---|---|
| `GET /api/<slug>/folders/` | the project's top-level folders (`id`, `name`) |
| `GET /api/<slug>/patients/` | the project's patients and their modalities |
| `POST /api/<slug>/upload/` | `multipart/form-data`: `name`, `project` (id), `folder` (id), and the files: `cbct`, `ios_upper`, `ios_lower`, `panoramic`, `teleradiography` |

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -F name="Patient 001" -F project=7 -F folder=12 -F cbct=@scan.nii.gz \
  $YGG/api/maxillo-study/upload/
```

Uploading needs a role that can write to the folder (`annotator` or `admin`).
The upload endpoint currently stores **maxillo** patients only, so don't use it
on projects in other domains.

## Documentation

- [docs/setup.md](docs/setup.md) — first-time setup: `.env`, `DOCKER_SUFFIX`, Docker networks
- [docs/running.md](docs/running.md) — day-to-day commands: start/stop, logs, migrations, shell access
- [docs/architecture.md](docs/architecture.md) — how the pieces fit: apps, request lifecycle, pipeline, annotation model
- [docs/urology_domain_architecture.md](docs/urology_domain_architecture.md) — urology domain architecture, multimodal design, and WSI pipeline
- [docs/wsi_viewer_process.md](docs/wsi_viewer_process.md) — WSI digital pathology pipeline from in-browser conversion to 40× visualization
- [docs/runners.md](docs/runners.md) — distributed runners and the runner callback API
- [docs/admin-tasks.md](docs/admin-tasks.md) — production operations: superusers, backups, maintenance modes, sweeps
- [docs/new-project-type.md](docs/new-project-type.md) — adding a new project app
- [CONTRIBUTING.md](CONTRIBUTING.md) — Docker quickstart, tests, CI, migrations, and the invariants
- [STUDENTS.md](STUDENTS.md) — where new work goes: free, through review, or maintainers only
- [CLAUDE.md](CLAUDE.md) — orientation for AI coding agents

Each app also carries its own README describing what it owns and where its
boundary with `common/` runs: [common](common/README.md),
[annotations](annotations/README.md), [maxillo](maxillo/README.md),
[brain](brain/README.md), [laparoscopy](laparoscopy/README.md),
[urology](urology/README.md), [cardiology](cardiology/README.md).

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
