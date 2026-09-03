# Architecture

Yggdrasil is a Django 5.2 platform for medical-imaging research: clinicians and
annotators upload studies, an external compute cluster processes them, and the
results are viewed, annotated and exported. This document is the map — what the
pieces are, where a boundary runs, and which paths are load-bearing. The rules
that are easy to break silently live in [CONTRIBUTING.md](../CONTRIBUTING.md);
this file explains the shape they protect.

## The five apps

| App | Owns | URL prefix |
|---|---|---|
| `common/` | Infrastructure shared by everything: `Project`, `Job`, `ProcessingStep`, `Modality`, `FileRegistry`, `ProjectAccess`, object storage, permissions, export, the runner worker, backups, site maintenance | mounted at `/` (landing, admin surfaces, `/api/…`) |
| `annotations/` | The durable, versioned annotation model — sets, targets, selectors, revisions, payloads | no prefix of its own; called from the domain apps |
| `maxillo/` | Dental / maxillofacial imaging (CBCT, IOS, intraoral photos, teleradiography, panoramic) | `/maxillo/` |
| `brain/` | Brain-tumour MRI (T1, T1c, T2, FLAIR, segmentation) | `/brain/` |
| `laparoscopy/` | Surgical video | `/laparoscopy/` |

`maxillo`, `brain` and `laparoscopy` are **domains**. A domain is registered in
exactly one place, `common/domains.py` (`DOMAIN_CHOICES`, `DOMAIN_FK_FIELDS`);
permissions and job routing derive from that registry, so adding a domain is a
registry entry plus per-domain FK columns on the three shared tables — never a
new `if domain == …` branch. See [new-project-type.md](new-project-type.md).

Each domain app owns its own `Patient`, `Folder`, `FolderAccess`, `Dataset`,
`Tag`, `VoiceCaption` and `Export` tables, most of them subclasses of the
abstract bases in `common/base_models.py`. `Patient` is deliberately *not*
based on a shared abstract model: it is the most domain-specific model in the
system and each app writes its own.

### Import direction

```
domain apps  ──▶  annotations  ──▶  common
     │                                 ▲
     └─────────────────────────────────┘
```

**`common/` must never import a domain app.** It is infrastructure that every
domain already depends on; an import back the other way is a cycle. Where
`common` needs domain data it goes through the registry (`fk_fields_for`,
`DomainFKAccessorMixin.get_patient()`) or `apps.get_model(...)`, never a direct
import.

### Why `annotations/` is not part of `common/`

`annotations/` depends on `common` (patients, `FileRegistry`, projects), and
`common` grew subsystems that want to ask questions *about* annotations. Had
annotations lived inside `common`, that would be a cycle inside one app with
nothing to stop it. Keeping them as two apps makes the direction checkable.

The rule, stated without the history: **`annotations` may import `common`;
`common` may not import `annotations`.** Where `common` needs to know whether
something is annotated, it asks through a narrow, stable module
(`common/annotation_lock.py`) rather than reaching into the annotation models.

Each app carries its own `README.md` naming what it owns, what it must not own,
and where its boundary with `common/` runs.

## Request lifecycle

`yggdrasil/settings.py` runs Django's own stack, then six project middlewares in
this order:

```
request
  │
  ├─ RequestLoggingMiddleware     stamps a request id; logs method/path/status/duration,
  │                               and unhandled exceptions
  │
  ├─ SiteMaintenanceMiddleware    reads the SiteMaintenance singleton (normal / read_only /
  │                               lockdown). Staff bypass. /static/, /login/, /logout/,
  │                               /maintenance/, /healthz and every /api/runner/ path are
  │                               exempt — runners must keep reporting during maintenance.
  │                               Answers 503 (lockdown) or 423 (read-only writes),
  │                               as JSON when the caller expects JSON.
  │
  ├─ ProjectSessionMiddleware     keeps session['current_project_id'] pointing at a project
  │                               *of the domain being browsed*. Only acts under the three
  │                               domain prefixes. The session project is domain-scoped:
  │                               crossing domains must re-resolve it.
  │
  ├─ ActiveProfileMiddleware      resolves ProjectAccess for (user, current project) and
  │                               sets request.user.profile, request.user_role,
  │                               request.user_project_access. Redirects to / when the user
  │                               has no access. Auto-provisions access for staff.
  │
  ├─ DemoGuestReadOnlyMiddleware  hard read-only backstop for the shared public-demo guest
  │
  └─ PresenceMiddleware           records last-seen for the "who is online" surface
  │
  ▼ view
```

Two consequences worth internalising:

- **`request.user.profile` is created by `ActiveProfileMiddleware`, and only
  under `/maxillo/`, `/brain/` and `/laparoscopy/`.** Anything under the global
  `/api/` namespace never gets it. A view there that reads `user.profile` will
  `AttributeError`, and — more dangerously — a view there that *assumes*
  middleware did an access check has no access check at all. **The view's own
  check is the only gate outside the domain prefixes.**
- **UI hiding is cosmetic.** Every gate that hides a control must be matched by
  a server-side refusal in the write endpoint. The same goes for modality
  gating on upload.

Authorization is `Project` + `ProjectAccess` (roles: viewer / annotator /
admin), resolved through `common/permissions.py`. A namespace is "the projects
whose `domain` equals this slug" — `Project.domain`, not `Project.slug`. The
`FolderAccess` tables still exist and still carry rows, but **they are not read
for authorization**.

## The processing pipeline

Upload is the only entry point; everything after it is driven by rows.

```
  browser upload
        │
        ▼
  common/uploads.py ──── writes bytes to object storage (key prefix derived from
        │                the DOMAIN, not the project slug)
        ▼
  FileRegistry row      (file_path = the object key; unique; sha256; modality; domain;
        │                per-domain patient FK)
        ▼
  Job row(s)            one per enabled ProcessingStep of the modality.
        │               Steps form a DAG: ProcessingStep.depends_on wires
        │               Job.dependencies, and a job pulls its inputs from the
        │               outputs of the jobs it waits on.
        ▼
  post_save signal ──── common/signals.py::_job_post_save
        │               Fires when a Job is created (or flipped back) into
        │               'pending'/'retrying'. Skips disabled modalities.
        │               Picks a queue via common/job_routing.py:
        │                 ProcessingStep.queue_name  (DB, wins over everything)
        │                 → RUNNER_QUEUE_BY_MODALITY / RUNNER_QUEUE_BY_PROJECT
        │                 → RUNNER_DEFAULT_QUEUE
        ▼
  celery_app.send_task(RUNNER_TASK_NAME, args=[job.id], queue=…)
        │               ← Redis
        ▼
  runner worker         common/runner/ — a dedicated Celery worker, NOT the web
        │               container. Holds the object-storage credentials and the
        │               SSH key. It claims/completes the job over the HTTP runner
        │               API, never through the ORM.
        │
        ├── claim ─────────────▶ POST /api/runner/jobs/<id>/claim/
        │
        ├── common/runner/ssh.py: one SSH connection per job to the SLURM login
        │   node; stages a transient 0600 credentials file over SFTP; submits
        │   ALGO_BASE_DIR/<ProcessingStep.algo_name>/run.sbatch; polls sacct
        │   until a terminal state.
        │
        │        ┌───────────────────────────────────────────────┐
        │        │  SLURM job on the cluster                     │
        │        │  pulls its own inputs from object storage,    │
        │        │  runs the algorithm, pushes its outputs back  │
        │        └───────────────────────────────────────────────┘
        │        No job bytes ever pass through the runner worker.
        │
        └── complete/fail ─────▶ POST /api/runner/jobs/<id>/{complete,fail}/
                                 output_files (object key map) + logs
                                       │
                                       ▼
                                 Job.output_files, status, FileRegistry rows for
                                 the *_processed artifacts; dependent jobs unblock.
```

Notes that matter when you touch this:

- **Dispatch is signal-driven. There is no `.delay()` anywhere in the tree** —
  the single call is `celery_app.send_task` in `common/signals.py`. Anything
  that needs a job run creates or re-pends a `Job` row and lets the signal do
  the rest; that is exactly what `manage.py resubmit_jobs` does. Bypassing the
  signal bypasses the modality kill switch and the routing table.
- The web app knows nothing about SLURM and never imports `common/runner/ssh.py`.
  A `ProcessingStep` with a blank `algo_name` stays on the plain Celery path.
- `Job.slurm_job_id` is observability only; the web app never reads it.
- The `maintenance` Celery queue (nightly backups) must never overlap
  `RUNNER_DEFAULT_QUEUE` or any `RUNNER_QUEUE_BY_*` value, or an external
  runner will consume backup jobs. Settings refuse to start on a collision.

### The runner HTTP contract is frozen

`maxillo/api_views/runner.py` + `maxillo/runner_api_service.py` expose
claim / attach / complete / fail, bearer-token authenticated against
`RUNNER_API_TOKENS` (constant-time compare; 503 when no tokens are configured,
401 on a bad one, 404 on an unknown job, 409 on a double claim). Runners
deployed outside this repository speak it.

**Its behaviour is pinned by `maxillo/tests_runner_api.py`, and those tests are
not editable without maintainer sign-off.** A failing contract test means
deployed runners break. Endpoint shapes are in [runners.md](runners.md).

## Object storage

All patient bytes live in an S3-compatible store (Garage in production and in
the dev stack), reached only through `common/object_storage.py` — a thin boto3
wrapper (`get_object_storage()`, `exists`, `list_keys`, `head`, upload/download,
presigning, a streaming download context manager). Keys are recorded in
`FileRegistry.file_path`. **The database holds rows; the store holds bytes.** A
database dump without the bucket gives you working patient pages and 404ing
downloads.

Key prefixes are derived from the **domain**, not the project slug. Do not
change that: prefixes only apply to new uploads, so a change silently splits one
patient's files across two layouts.

Anything that reads bytes out of the store in bulk is a **management command** —
never a `RunPython` migration (row counts are unbounded, it blocks the deploy,
it cannot resume, and object storage is unreachable in CI) and never a request
path. The required shape is in [CONTRIBUTING.md](../CONTRIBUTING.md).

## The annotation model

`annotations/` is the durable record. Everything an annotator produces —
landmarks, tooth segmentation, occlusion classification, panoramic arches, video
regions and quadrant markers, volume segmentation, measurements, voice captions
— is stored here in one shape.

```
SourceResource                     the thing annotated, addressed by a stable
  identity_key (unique, varchar255) "<kind>:<parts>" key that is a database key,
  kind, content_hash                not a URL. Never derived from a Cornerstone id.
        ▲
        │ PROTECT
AnnotationSet ──1:N──▶ AnnotationTarget ──1:N──▶ AnnotationSelector
  kind, domain,          role ('volume',           kind, coordinate_system
  patient FK (x3),       'segmentation', …)        (lps | ras | volume_voxel |
  annotation_method,     primary_slot (1 or NULL)   resource_local | none),
  label_schema,          status                     frame_index, slice_axis/index,
  status, ever_annotated                            start/end_time_ms (integers),
        │                                           segment_value
        │ 1:N
        ▼
AnnotationRevision                 one saved state. Snapshots, not deltas.
  revision_number  ◀── UniqueConstraint(annotation_set, revision_number)
  origin (manual | predicted | …)
  author, note
  source_fingerprint {identity_key: content_hash}
        │ 1:N
        ▼
AnnotationPayload                  one encoding of that state
  format, canonical_slot (1 or NULL), variant
  data (inline JSON)  |  file ──▶ common.FileRegistry (dense artifacts)
        │
        ├── AnnotationItem subclasses hang off the revision:
            Geometry2DItem, SpatialAnnotation3DItem, MeasurementItem,
            TemporalAnnotationItem, EventAnnotationItem
```

Bytes never live in an annotation table. A dense labelmap is a `FileRegistry`
row addressed by an `AnnotationPayload`; a sparse annotation is rows.

### The four layers

```
views / API  ─▶  services/  ─▶  models
                  ▲     ▲
        adapters/─┘     └─validators/          serializers/ ─▶ canonical JSON
```

- **`validators/` is pure.** Values in, `ValidationError` out. No database, no
  object storage, no model instances — that is what lets one rule run in a
  service, in a management command sweeping legacy rows, and in a test with a
  literal dict.
- **`adapters/` is pure translation.** A legacy row or an interchange document
  in, descriptor dicts out. It never queries and never saves.
- **`services/` is the only writer.** A view that imports an annotation model
  and calls `.save()` is a review failure. One transaction allocates the
  revision number, refreshes the monotonic `ever_annotated` flag, fingerprints
  the targets and validates the items.
- **`serializers/` builds the canonical JSON document.** Exports *render* that
  document from the record; nothing reads a stored one back as truth.

Concurrency: pass the revision you loaded to `record_revision`. Never compute
the next number from `SELECT MAX(...)+1` — that reopens the race
`UniqueConstraint(annotation_set, revision_number)` exists to close. A losing
writer gets a conflict and rebases; there is deliberately no automatic merge,
because union / intersection / last-writer-wins are each clinically wrong
somewhere and a silent wrong merge is worse than an explicit conflict.

Two more properties that surprise people:

- **A save replaces the whole set**, so deleting the last measurement is an
  empty save. Saves that span several targets must carry forward the targets
  they did not name — except on single-owner surfaces, where carrying forward
  copies stale state onto the new revision.
- **Revision numbers are monotonic and never rewind.** "The source changed,
  start again" gives the *client* a fresh count while the server keeps its own;
  only the effective revision is quoted back.
- The **annotation lock** (`common/annotation_lock.py`) is monotonic: once a
  patient's raw data has been annotated it stays locked, so the bytes under a
  revision cannot move. A superuser override stamps `metadata['lock_override']`
  so a later fingerprint mismatch is explainable.

## Imaging frontend

All imaging runs on **Cornerstone3D**, built from `frontend/` with npm +
esbuild into a committed bundle under `static/vendor/cornerstone/`. Both npm and
esbuild are dev-only: deploys need no Node. Templates load a surface with
`{% cornerstone_entry '<name>' %}` (`common/templatetags/cornerstone.py`), which
emits `type="module"` and has no non-module variant — three vendored packages
resolve their web workers through `import.meta.url`, which does not exist in an
IIFE build.

Volumes are **NIfTI (`.nii.gz`)**. Volume URLs are built by
`frontend/imaging/ids/imageIds.js`, which has rules the loader forces on it: the
URL must be absolute, the `.gz` suffix must be on the last path segment, and
`file_key` must stay a query parameter.

`static/js/seg2pano_core.js` and its worker are the panoramic reconstruction
(arch fit, slab, projection). They are **not** migrated code and must not become
it: the bundle reaches them through the `Seg2PanoCore` global on purpose.

The build, the CI gate on it, and the constraints that look like style but are
not are documented in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Where things live

| Concern | Module |
|---|---|
| Domain registry | `common/domains.py` |
| Authorization | `common/permissions.py`, `common.ProjectAccess` |
| Job queue selection | `common/job_routing.py` |
| Job dispatch | `common/signals.py::_job_post_save` |
| Cluster execution | `common/runner/` (worker only) |
| Object storage | `common/object_storage.py` |
| Upload → FileRegistry | `common/uploads.py` |
| Export | `common/export_catalog.py`, `common/export_processing.py`, `common/export_share.py` |
| Site maintenance modes | `common.SiteMaintenance`, `yggdrasil/middleware.py` |
| Annotation writes | `annotations/services/` |
| Raw-data lock | `common/annotation_lock.py` |
| Backups | `common/tasks.py`, `manage.py backup_now` |
| Middlewares | `yggdrasil/middleware.py` |
| Bundle assets | `common/cornerstone_assets.py`, `common/templatetags/cornerstone.py` |
