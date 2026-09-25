# Adding a new project type

A "project type" is a Django app like `maxillo/`, `brain/`, `laparoscopy/`, `urology/`, or `cardiology/` — each is a separate imaging workflow (its own patients, modalities, folders) mounted under its own URL prefix. There's no generator for this: the current practice is to copy an existing app and wire it into the shared code. Be realistic about the size: adding urology touched about 30 shared files outside `urology/`, and `common/tests_domain_registry.py` exists because some were still missed. Run it (step 7) — it lists what a domain must provide.

Most of the per-domain wiring is driven by a single registry (`common/domains.py`) and a set of abstract base models (`common/base_models.py`), so there is far less hardcoded branching than there used to be. The remaining hardcoded piece is the per-app FK columns on the shared `Job`/`ProcessingJob`/`FileRegistry` tables and on `annotations.AnnotationSet` (see step 4).

Use `laparoscopy/` as your template if your app can reuse generic patient/folder/export views (lighter weight). Use `brain/` or `urology/` as your template if you need dedicated viewers, `app_urls.py` or specialized modality readers.

## 1. Pick a domain name

Pick a short lowercase slug, e.g. `endo`. It becomes the Django app label, the `Project.slug`, and the `domain` value stored on shared tables. Used consistently everywhere below.

## 2. Create the app

```bash
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py startapp endo
```

Give it the same shape as `brain/`/`laparoscopy/`:

- `apps.py` — standard `AppConfig`
- `models.py` — your own `Patient`, `VoiceCaption`, `Export`, `Folder`, `FolderAccess`, `Tag`, and (optionally) `Classification`. **Subclass the abstract bases in `common/base_models.py`** instead of copy-pasting fields/methods — copy `brain/models.py` as a starting point since it already does this. Each subclass only needs to carry:
  - the fields that legitimately differ per app (the `user`/`created_by`/`annotator` `related_name`, `db_table`, any domain-specific fields, and per-app `help_text` drift),
  - its own `class Meta` (always set `db_table = 'endo_<model>'`),
  - `__str__` where it differs from the base.

  The shared behavior — `VoiceCaption`'s full transcription/`files`/`processing_jobs` method set, `Export`'s `mark_*`/`ensure_share_token`/expiry, `Folder.get_full_path`, `FolderAccess` roles, `ActivePatientManager`, etc. — comes from the base for free. Unqualified relations in the bases (e.g. `ForeignKey('Patient')`, `ForeignKey('Folder')`) resolve against **your** app automatically. See `brain/models.py` / `laparoscopy/models.py` for the exact subclass pattern.

  `Patient` is intentionally **not** based on a shared abstract model — it is the most domain-specific model (different modality fields, scan-status logic, and helper methods per app), so you write it yourself. Only its soft-delete manager is shared: `objects = ActivePatientManager()` imported from `common.base_models`.
- `urls.py` (+ `app_urls.py` if you need dedicated views, like `brain/`)
- `forms.py`, `views.py`, `admin.py`
- `management/commands/setup_endo_modalities.py` — see step 5

## 3. Register the app

- `yggdrasil/settings.py` — add `"endo"` to `INSTALLED_APPS`.
- `yggdrasil/urls.py` — add `path("endo/", include("endo.urls"))` next to the `brain`/`laparoscopy` lines.
- `pyproject.toml` — add `"endo"` to import-linter's `root_packages` and to the domain layer (`"maxillo : brain : … : endo"`). `lint-imports` then holds your app to the import direction; where shared code needs something only your app can compute, register it from your `AppConfig.ready()` (e.g. `common.export_catalog.register_collector` for a database-backed export artifact) instead of importing your app from `common/`.

## 4. Register the domain (`common/domains.py` + `common/models.py`)

This is the one piece that still needs a shared-table edit — the `Job`, `ProcessingJob`, `FileRegistry` and `annotations.AnnotationSet` tables use a `domain` string discriminator **plus** a nullable FK per app (`brain_patient`, `laparoscopy_patient`, ...) rather than a generic relation.

1. **`common/domains.py`** — add your domain in one place:
   - append `("endo", "Endo")` to `DOMAIN_CHOICES`;
   - add `"endo": ("endo_patient", "endo_voice_caption")` to `DOMAIN_FK_FIELDS`.

   `DOMAINS`, `normalize_domain`, `fk_fields_for`, and the `Job`/`FileRegistry` `get_patient()`/`set_patient()` accessors all derive from these, so once this is done the registry, permissions, and job routing pick your domain up automatically.

2. **`common/models.py`** — on each of `Job`, `ProcessingJob`, `FileRegistry` add the matching FK columns (and the patient FK on `annotations/models/sets.py` `AnnotationSet`):
   ```python
   endo_patient = models.ForeignKey('endo.Patient', on_delete=models.CASCADE, related_name='...', null=True, blank=True)
   endo_voice_caption = models.ForeignKey('endo.VoiceCaption', on_delete=models.CASCADE, related_name='...', null=True, blank=True)
   ```
   (`DOMAIN_CHOICES` is imported from `common.domains` — do **not** redefine it here.)

Then generate the migrations. The `annotations` migration that adds `AnnotationSet.endo_patient` must list the `common` migration adding your FileRegistry/Job/ProcessingJob columns among its dependencies (`makemigrations` usually adds it; keep it). On a fresh database that edge is what applies your columns before `annotations/0005`, which reads those tables through the live models. Never add the dependency to `0005` itself: production has it applied, and `migrate` would stop with `InconsistentMigrationHistory`.

```bash
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py makemigrations
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py migrate
```

## 5. Write the modality bootstrap command

Copy `laparoscopy/management/commands/setup_laparoscopy_modalities.py` (or the `brain` one) into `endo/management/commands/setup_endo_modalities.py`. It's copy-paste by design — there's no shared base class. It should:

1. `Project.objects.get_or_create(slug='endo', defaults={...})`
2. Define your `modalities_data` list (slug, supported extensions, etc.)
3. `Modality.objects.get_or_create(...)` per entry, then `project.modalities.add(modality)`

Run it after migrating (see [docs/setup.md](setup.md)):

```bash
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py setup_endo_modalities
```

Processing is configured per modality with `common.ProcessingStep` rows (admin: queue, `algo_name`, blocking/discard flags, the step DAG). A modality without a step row has no processing.

## 6. Permissions & job routing — nothing to branch

Both `common/permissions.py` and `common/job_routing.py` are **registry-driven** and need no per-domain edits:

- Authorization is per `Project` through `ProjectAccess` (viewer / annotator / admin). The legacy `FolderAccess` tables are **not** read for authorization. Every object check takes the object's own project: load patients with `get_patient_for(user, Patient, id, perm)`, never pass `request` or a domain name to a permission helper (they raise `TypeError`). `current_project(request)` is the session's project for UI defaults only.
- `_project_slug_for_job()` resolves the job's patient via `fk_fields_for(domain)` — the FK columns you added in step 4 are all it needs.

## 7. Sanity check

- `python manage.py test common.tests_domain_registry common.tests_cross_project_matrix` — the first checks the models, FK columns and shared URL names a domain must provide; the second requests every object URL of every domain as an admin of another project and must pass for yours.
- Upload a file through your new app's UI/API and confirm a `FileRegistry` row is created with `domain='endo'` and `endo_patient` set.
- Confirm project access applies (test as a non-staff user with `ProjectAccess` on an `endo` project only).
- If using distributed runners, confirm jobs route to a queue ([docs/runners.md](runners.md)) via `common/job_routing.py`.
- Run `python manage.py makemigrations --check --dry-run` — your new app's migration should be the *only* change; the shared/base-model refactor must not have dirtied any existing table.
