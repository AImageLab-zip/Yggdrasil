"""Central accessors for admin-driven processing config (Phase 4).

Per-modality processing is now declared as ``ProcessingStep`` rows (a modality
can have several, forming a DAG). Every reader is keyed by a **step slug**,
which is what ``Job.modality_slug`` carries: a modality's *root* step has
``slug == modality.slug``, so passing a plain modality slug resolves that root
step, while a downstream step (e.g. ``ios_orientation``) resolves itself.

A slug with **no** step row declares no pipeline, and that is not a gap to be
filled by a fallback: the runner sbatches ``ALGO_BASE_DIR/<algo_name>/run.sbatch``
and ``common.runner.run.run_job`` fails a job whose step has no ``algo_name``, so
a Job created for a step-less modality can only ever end 'failed'. Every modality
that predates the step table was given a row by migration 0034 (disabled for the
ones that never processed), so "no row" means "declared after that, and never
given a step" -- an admin-added modality like a photo type that is uploaded and
read, never computed on.

This module holds the single source of truth for the enablement rule so that
``common.job_routing`` can delegate here without circular recursion.
"""
import logging

from django.db.utils import DatabaseError

logger = logging.getLogger(__name__)


def get_step(slug):
    """Return the ProcessingStep whose slug matches, or None if absent.

    Swallows DatabaseError (e.g. a read before the table is migrated) and
    reports it as absent, which is the safe direction: callers then create no
    Job rather than one no runner can execute.
    """
    slug = str(slug or "").strip()
    if not slug:
        return None
    from common.models import ProcessingStep
    try:
        return (
            ProcessingStep.objects.select_related("modality")
            .filter(slug=slug)
            .first()
        )
    except DatabaseError:
        logger.warning("ProcessingStep lookup failed for '%s'; using legacy fallback", slug)
        return None


def modality_requires_processing(modality_slug):
    """Whether an upload for this slug needs runner processing.

    True iff an enabled ``ProcessingStep`` declares it. No row means no
    pipeline, hence no Job: see this module's docstring for why a job for a
    step-less modality is not merely useless but always failing.
    """
    step = get_step(modality_slug)
    return step is not None and step.is_enabled


def modality_is_enabled(modality_slug):
    """Whether the runner is enabled for this step slug.

    Same rule as :func:`modality_requires_processing` -- a step declares the
    work and its own enablement, and nothing else can. Kept as its own name
    because ``common.job_routing`` asks the routing question, not the upload
    one.
    """
    return modality_requires_processing(modality_slug)


def queue_override_for(modality_slug):
    """DB queue override for this step slug, or None when unset.

    A non-blank value wins over ALL env routing (maintainer decision).
    """
    step = get_step(modality_slug)
    if step is not None and step.queue_name and step.queue_name.strip():
        return step.queue_name.strip()
    return None


def modality_is_blocking(modality_slug):
    """Whether an in-flight job gates patient readiness (shows 'processing')."""
    step = get_step(modality_slug)
    if step is not None:
        return step.is_blocking
    return modality_requires_processing(modality_slug)


def modality_status(modality_slug, jobs, has_files):
    """The patient-row status for one modality's pill.

    ``'failed' | 'processing' | 'pending' | 'processed' | 'absent'``, in that
    precedence, from the patient's Jobs for this slug and whether it has any
    file.

    **A modality that declares no enabled step is read from its files alone.**
    It has no processing, so it has no processing status to report: one upload
    makes it green. That is also what clears the debris -- a patient uploaded
    while a step-less modality still spawned a Job carries a 'failed' row that
    no rerun can ever complete, because there is no algo to run.
    """
    if modality_requires_processing(modality_slug):
        statuses = {getattr(job, "status", "") for job in jobs or ()}
        if "failed" in statuses:
            return "failed"
        if "processing" in statuses:
            return "processing"
        if statuses & {"pending", "retrying"}:
            return "pending"
    return "processed" if has_files else "absent"


def modality_discard_raw(modality_slug):
    """Whether this modality's raw input files are hidden as a security screen.

    True only when a step row exists and has ``discard_raw`` set; absent a step
    row we stay permissive (legacy behavior)."""
    step = get_step(modality_slug)
    if step is not None:
        return bool(step.discard_raw)
    return False


def modality_prefers_processed_for_viewer(modality_slug):
    """Whether the modality viewer should prefer processed files over raw."""
    step = get_step(modality_slug)
    if step is not None:
        return bool(step.prefer_processed_for_viewer)
    return False


def _modality_slug_for_file(file_obj):
    """Best-effort modality slug for a FileRegistry row.

    Resolves via the modality FK, then a ``modality_slug`` in metadata, then the
    ``{slug}_raw`` file_type prefix. Returns '' when nothing resolves.
    """
    modality = getattr(file_obj, "modality", None)
    if modality is not None and getattr(modality, "slug", ""):
        return str(modality.slug).strip()
    metadata = getattr(file_obj, "metadata", None)
    if isinstance(metadata, dict) and metadata.get("modality_slug"):
        return str(metadata["modality_slug"]).strip()
    file_type = str(getattr(file_obj, "file_type", "") or "")
    if file_type in {"ios_raw_upper", "ios_raw_lower"}:
        return "ios"
    if file_type.endswith("_raw"):
        return file_type[: -len("_raw")]
    return ""


def _file_is_raw(file_obj):
    file_type = str(getattr(file_obj, "file_type", "") or "")
    return (
        file_type.endswith("_raw")
        or file_type in {"ios_raw_upper", "ios_raw_lower", "rgb_image"}
    )


def _processed_exists_for(file_obj, slug):
    """Whether a ``{slug}_processed`` file exists for the same owning patient.

    Prefix match (not exact) so this also finds legacy suffixed rows written
    before the generic write path (e.g. ``ios_processed_upper``/``_lower``)
    alongside the plain ``{slug}_processed`` rows new completions write.
    """
    if not slug:
        return False
    from common.models import FileRegistry
    owner_fields = ("patient", "brain_patient", "laparoscopy_patient")
    filters = {}
    for field in owner_fields:
        owner = getattr(file_obj, f"{field}_id", None)
        if owner:
            filters[field] = owner
            break
    if not filters:
        return False
    try:
        return FileRegistry.objects.filter(
            file_type__startswith=f"{slug}_processed", **filters
        ).exists()
    except DatabaseError:
        return False


def raw_file_hidden(file_obj):
    """Whether a raw FileRegistry row must NOT be listed or served.

    Central gate shared by the file listing, the per-modality data endpoints and
    the serve_file backstop. Only raw inputs are affected:
      - ``discard_raw`` on the modality's step  -> always hidden.
      - ``is_blocking`` on the step AND no ``{slug}_processed`` file yet -> hidden
        until processing produces a processed output.
    Non-raw files, and modalities without a step row, are never hidden here.
    """
    if not _file_is_raw(file_obj):
        return False
    slug = _modality_slug_for_file(file_obj)
    step = get_step(slug)
    if step is None:
        return False
    if step.discard_raw:
        return True
    if step.is_blocking and not _processed_exists_for(file_obj, slug):
        return True
    return False


def _available_steps_for_files(patient_files, patient=None):
    """Enabled ProcessingSteps reachable from a patient's raw inputs.

    Mirrors the upload-time pipeline in ``common.uploads.create_step_jobs``:
    a *root* step (no ``depends_on``) is available when the patient has any
    input file for its modality, and every other step is available only once
    all of its prerequisites are available. This is what makes e.g. IOS
    Landmarks impossible for a patient without an IOS scan, and IOS Bite
    Classification impossible without IOS Landmarks, purely from the
    admin-declared ``depends_on`` DAG.

    When ``patient`` is given, steps whose modality the patient's project does
    not enable are excluded (project processing scoping).

    Returns a list of ``ProcessingStep`` objects in topological order
    (prerequisites before dependents).
    """
    from common.models import ProcessingStep

    steps = list(
        ProcessingStep.objects.filter(is_enabled=True)
        .select_related("modality")
        .prefetch_related("depends_on")
    )
    if not steps:
        return []

    project = getattr(patient, "project", None) if patient is not None else None
    if project is not None:
        allowed_slugs = set(project.modalities.values_list("slug", flat=True))
        if allowed_slugs:
            steps = [s for s in steps if s.modality.slug in allowed_slugs]
        disabled_slugs = set(project.disabled_steps.values_list("slug", flat=True))
        if disabled_slugs:
            steps = [s for s in steps if s.slug not in disabled_slugs]
        if not steps:
            return []

    files_by_modality = {}
    for file_obj in patient_files or []:
        slug = _modality_slug_for_file(file_obj)
        if slug:
            files_by_modality.setdefault(slug, []).append(file_obj)

    available = {}
    progressed = True
    while progressed:
        progressed = False
        for step in steps:
            if step.slug in available:
                continue
            deps = list(step.depends_on.all())
            if deps:
                if all(d.slug in available for d in deps):
                    available[step.slug] = step
                    progressed = True
            elif files_by_modality.get(step.modality.slug):
                available[step.slug] = step
                progressed = True

    ordered = []
    added = set()
    pending = dict(available)
    while pending:
        progressed = False
        for slug in list(pending):
            step = pending[slug]
            if all(d.slug in added for d in step.depends_on.all()):
                ordered.append(step)
                added.add(step.slug)
                del pending[slug]
                progressed = True
        if not progressed:
            break
    return ordered


def rerunnable_steps_for_patient(patient_files, modality_status_list=None, patient=None):
    """Processing steps possible for a patient, for the rerun job picker.

    Returns ``[{"slug": ..., "name": ...}, ...]`` from the admin ``ProcessingStep``
    table (name column) in dependency order. When the domain declares no enabled
    steps, falls back to the historical per-modality list so non-pipeline apps
    (e.g. brain) keep showing their existing rerunnable modalities.
    """
    steps = _available_steps_for_files(patient_files, patient=patient)
    if steps:
        return [{"slug": step.slug, "name": step.name} for step in steps]

    fallback = []
    for entry in modality_status_list or []:
        slug = str(entry.get("slug", "") or "").strip()
        if slug in {"rawzip", "voice"} or entry.get("status") == "absent":
            continue
        fallback.append({
            "slug": slug,
            "name": entry.get("label") or entry.get("name") or slug,
        })
    return fallback


def rerun_step_labels(patient_files, modality_status_list=None):
    """Slug -> display-name map for the rerun modal checkboxes.

    Prefers admin ProcessingStep names (the *Name* column), falling back to the
    legacy modality labels when no steps are configured.
    """
    from common.models import ProcessingStep

    step_labels = dict(
        ProcessingStep.objects.filter(is_enabled=True).values_list("slug", "name")
    )
    if step_labels:
        return step_labels
    return {
        entry["slug"]: (entry.get("label") or entry.get("name") or entry["slug"])
        for entry in modality_status_list or []
        if entry.get("slug") not in {"rawzip", "voice"}
        and entry.get("status") != "absent"
    }
