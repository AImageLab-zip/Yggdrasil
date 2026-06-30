# Adding a new project type

A "project type" is a Django app like `maxillo/`, `brain/`, or `laparoscopy/` — each is a separate imaging workflow (its own patients, modalities, folders) mounted under its own URL prefix. There's no generator for this: the current practice is to copy an existing app and then touch a handful of shared files in `common/` to wire in the new domain. This isn't a clean plug-in system — several pieces are hardcoded per-domain rather than generic, noted below.

Use `laparoscopy/` as your template if your app can reuse generic patient/folder/export views (lighter weight). Use `brain/` as your template if you need your own `app_urls.py`/`api_views.py`.

## 1. Pick a domain name

Pick a short lowercase slug, e.g. `endo`. It will become the Django app label, the `Project.slug`, and the `domain` value stored on shared tables. Used consistently everywhere below.

## 2. Create the app

```bash
docker exec -it toothfairy4m-web-$DOCKER_SUFFIX python manage.py startapp endo
```

At minimum, give it the same shape as `brain/`/`laparoscopy/`:

- `apps.py` — standard `AppConfig`
- `models.py` — your own `Patient`, `VoiceCaption`, `Export`, `Dataset`, `Folder`, `FolderAccess`, `Tag` (copy `brain/models.py` and rename `brain_*` related_names/`db_table`s to `endo_*`)
- `urls.py` (+ `app_urls.py` if you need dedicated views, like `brain/`)
- `forms.py`, `views.py`, `admin.py`
- `management/commands/setup_endo_modalities.py` — see step 5

## 3. Register the app

- `toothfairy/settings.py` — add `"endo"` to `INSTALLED_APPS`.
- `toothfairy/urls.py` — add `path("endo/", include("endo.urls"))` next to the `brain`/`laparoscopy` lines.

## 4. Wire the new domain into shared tables (`common/models.py`)

This is the part that isn't generic — `Job`, `ProcessingJob`, and `FileRegistry` use a `domain` string discriminator (`DOMAIN_CHOICES`) **plus** a hardcoded nullable FK per app (`brain_patient`, `laparoscopy_patient`, ...) rather than a generic relation. For each of the three models:

1. Add `"endo"` to `DOMAIN_CHOICES`.
2. Add `endo_patient = models.ForeignKey('endo.Patient', on_delete=models.CASCADE, related_name='...', null=True, blank=True)` (and `endo_voice_caption` where `FileRegistry`/`ProcessingJob` have the brain/laparoscopy equivalent).
3. Update any `related_bits`/`__str__` helper that lists `brain_patient_id` etc. to also check `endo_patient_id`.

Then:

```bash
docker exec -it toothfairy4m-web-$DOCKER_SUFFIX python manage.py makemigrations
docker exec -it toothfairy4m-web-$DOCKER_SUFFIX python manage.py migrate
```

## 5. Write the modality bootstrap command

Copy `laparoscopy/management/commands/setup_laparoscopy_modalities.py` (or `brain/management/commands/setup_brain_modalities.py`) into `endo/management/commands/setup_endo_modalities.py`. It's copy-paste by design — there's no shared base class. It should:

1. `Project.objects.get_or_create(slug='endo', defaults={...})`
2. Define your `modalities_data` list (slug, supported extensions, etc.)
3. `Modality.objects.get_or_create(...)` per entry, then `project.modalities.add(modality)`

Run it after migrating (see [docs/setup.md](setup.md)):

```bash
docker exec -it toothfairy4m-web-$DOCKER_SUFFIX python manage.py setup_endo_modalities
```

## 6. Extend permission helpers (`common/permissions.py`)

`_namespace()` and `_folder_access_model()` in `common/permissions.py` only branch between `"brain"` and `"maxillo"` — anything else falls through to `"maxillo"`. This is how `laparoscopy` gets away with having no `FolderAccess` model of its own (it silently reuses maxillo's). If your app has its own `FolderAccess` model (recommended, like `brain`), you must extend these branches to recognize `"endo"` explicitly, or your folder ACLs will silently apply maxillo's rules instead of yours.

## 7. Extend job routing (`common/job_routing.py`)

`_project_slug_for_job` has an explicit `if domain == "brain": ... elif domain == "laparoscopy": ...` chain. Add an `elif domain == "endo":` branch so background jobs for your app route to the right project/queue.

## 8. Sanity check

- Upload a file through your new app's UI/API and confirm a `FileRegistry` row is created with `domain='endo'` and `endo_patient` set.
- Confirm folder access rules apply your app's `FolderAccess` model, not maxillo's (test as a non-staff user with only `endo` folder access).
- If using distributed runners, confirm jobs route to a queue ([docs/runners.md](runners.md)) and look up the right project via `common/job_routing.py`.
