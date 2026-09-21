"""Queueing a patient's processing steps — the one implementation.

Every domain's ``rerun_processing`` / ``bulk_rerun_processing`` view delegates
here. The views keep their own permission checks (each domain decides who may
rerun) and their own response shaping; the work itself is domain-agnostic.

The name "rerun" is historical and undersells it: a step is queued whether or
not it has ever run for this patient. ``common.uploads.ensure_step_jobs_for_patient``
creates the missing ``Job`` rows (and their prerequisite closure) first, so a
``ProcessingStep`` registered *after* a patient was uploaded runs on that patient
the first time it is picked. That is what makes "I just registered a new
algorithm, run it over the back catalogue" work, and it pairs with
``common.modality_config.rerunnable_steps_for_patient``, which decides what to
offer from the patient's raw files and the step DAG rather than from existing
jobs.

Domain-agnostic by construction: the patient's own FK column and domain come
from ``common.uploads.entity_fk_kwargs``, which reads the domain registry. There
is no per-domain branch here, and adding a domain needs no edit.
"""

import logging

logger = logging.getLogger(__name__)

#: Caption "modalities" are user recordings, not pipeline steps; they have no job.
NON_PIPELINE_SLUGS = frozenset({"audio", "voice"})


def _job_filter(patient):
    """``{"domain": ..., "<patient_fk>": patient}`` for this patient's Job rows."""
    from common.uploads import entity_fk_kwargs

    kwargs = entity_fk_kwargs(patient)
    return {k: v for k, v in kwargs.items() if k == "domain" or v is not None}


def _normalize(requested_slugs):
    """Trimmed, de-duplicated, pipeline-only slugs, in the order given."""
    seen = set()
    slugs = []
    for raw in requested_slugs or []:
        slug = str(raw or "").strip()
        if not slug or slug in seen or slug in NON_PIPELINE_SLUGS:
            continue
        seen.add(slug)
        slugs.append(slug)
    return slugs


def rerun_steps_for_patient(patient, requested_slugs):
    """Queue ``requested_slugs`` for ``patient``.

    Creates any missing jobs, then resets the latest job per step to ``pending``
    so a worker picks it up. Returns ``{"updated": [...], "created": [...],
    "not_found": [...]}`` — ``created`` are steps that had never run before.
    """
    from common.models import Job
    from common.uploads import ensure_step_jobs_for_patient

    slugs = _normalize(requested_slugs)
    if not slugs:
        return {"updated": [], "created": [], "not_found": []}

    job_filter = _job_filter(patient)

    # Before resetting: give newly-registered steps (and their prerequisites) a
    # job, so this is a first run rather than a no-op for them.
    created = sorted({job.modality_slug for job in ensure_step_jobs_for_patient(patient, slugs)})

    updated = []
    not_found = []
    for slug in slugs:
        try:
            job = (
                Job.objects.filter(modality_slug=slug, **job_filter)
                .order_by("-created_at")
                .first()
            )
        except Exception:
            logger.exception("Error loading job %s for patient %s", slug, patient.pk)
            not_found.append(slug)
            continue

        if job is None:
            # No job even after ensure_*: the step is not enabled for this
            # patient's project, or its root modality has no input file.
            not_found.append(slug)
            continue

        job.status = "pending"
        job.started_at = None
        job.completed_at = None
        job.worker_id = ""
        job.error_logs = ""
        job.save()

        # Dependent steps go back to `dependency` until their inputs are ready.
        if hasattr(job, "update_status_based_on_dependencies"):
            job.update_status_based_on_dependencies()

        updated.append(slug)

    return {"updated": updated, "created": created, "not_found": not_found}


def bulk_rerun_steps(patients, requested_slugs):
    """``rerun_steps_for_patient`` across several patients, aggregated.

    Returns per-step counts alongside the totals, which is what the patient-list
    bulk picker reports back to the user.
    """
    slugs = _normalize(requested_slugs)

    updated_pairs = 0
    not_found_pairs = 0
    updated_by_step = {}
    not_found_by_step = {}
    created_slugs = set()

    for patient in patients:
        result = rerun_steps_for_patient(patient, slugs)
        created_slugs.update(result["created"])
        for slug in result["updated"]:
            updated_pairs += 1
            updated_by_step[slug] = updated_by_step.get(slug, 0) + 1
        for slug in result["not_found"]:
            not_found_pairs += 1
            not_found_by_step[slug] = not_found_by_step.get(slug, 0) + 1

    return {
        "requested": slugs,
        "updated_pairs": updated_pairs,
        "not_found_pairs": not_found_pairs,
        "updated_by_modality": updated_by_step,
        "not_found_by_modality": not_found_by_step,
        "created_slugs": sorted(created_slugs),
    }


def describe(result):
    """One-line human summary of a ``rerun_steps_for_patient`` result."""
    parts = []
    if result["created"]:
        parts.append(f"Created missing job for: {', '.join(result['created'])}")
    if result["updated"]:
        parts.append(f"Updated: {', '.join(result['updated'])}")
    if result["not_found"]:
        parts.append(f"No job found for: {', '.join(result['not_found'])}")
    return "; ".join(parts) if parts else "No changes made"
