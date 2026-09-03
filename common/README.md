# `common/`

The infrastructure every other app depends on. If two domains would each need
their own copy of a thing, it belongs here.

## What it owns

- **The domain registry** — `domains.py`. `DOMAIN_CHOICES` and `DOMAIN_FK_FIELDS`
  are the single source of truth for which domains exist; permissions, job
  routing and the landing page all derive from it.
- **Projects and authorization** — `Project`, `ProjectAccess` (viewer /
  annotator / admin), `Invitation`, and `permissions.py`. A namespace is "the
  projects whose `domain` equals this slug".
- **The processing pipeline** — `Modality`, `ProcessingStep`, `Job`,
  `ProcessingJob`, the dispatch signal in `signals.py`, queue selection in
  `job_routing.py`, and the runner worker under `runner/`.
- **Files** — `uploads.py` (bytes in), `FileRegistry` (the row that names them),
  `object_storage.py` (the only boto3 wrapper), `file_access.py`, `deletion.py`.
- **Export** — `export_catalog.py`, `export_processing.py`, `export_share.py`,
  `export_ui.py`.
- **Abstract bases the domains subclass** — `base_models.py`: `FolderBase`,
  `FolderAccessBase`, `DatasetBase`, `TagBase`, `ClassificationBase`,
  `ExportBase`, `VoiceCaptionBase`.
- **Cross-cutting site services** — site maintenance modes, presence, activity,
  notifications, recently-viewed, user preferences, backups (`tasks.py`), the
  status/health surfaces, the Cornerstone bundle template tag.

## What it must NOT own

- **Anything domain-specific.** No CBCT logic, no MRI sequences, no video
  handling, and no `Patient` model — `Patient` is deliberately not a shared
  abstract base, because it is the most domain-specific model in the system.
- **The annotation record.** That is `annotations/`.
- **An import of a domain app, or of `annotations`.** This is the hard rule:
  imports run domain apps → `annotations` → `common`, one way. `common/` is
  infrastructure everything else already depends on, so an import back the other
  way is a cycle.

## The boundary

Where `common` needs domain data, it goes through the registry
(`fk_fields_for()`, `DomainFKAccessorMixin.get_patient()`) or
`apps.get_model(domain, "Patient")` — never a direct import. Where it needs to
know whether something is annotated, it asks through one narrow, stable module,
`annotation_lock.py`, rather than reaching into annotation models.

The test for "does this belong in `common/`?" is not "do two apps use it" but
**"can it be written without naming a domain?"** A helper with a
`if domain == "maxillo"` branch in it belongs in `maxillo/`, or belongs in
`common/domains.py` as registry data.
