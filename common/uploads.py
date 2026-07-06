"""Shared upload primitives for all domain apps (maxillo, laparoscopy, brain).

Promoted from ``maxillo.file_utils`` in Phase 5.1 so sibling apps stop reaching
into maxillo private helpers. These are domain-agnostic: they resolve a Patient's
domain from its ``_meta.app_label`` and stream uploads to object storage.

The domain branching in ``entity_fk_kwargs``/``domain_for_patient`` is inherited
as-is; the registry-driven rewrite lands in Phase 5.2 (``common/domains.py``).
"""

import contextlib
import hashlib
import os
import tempfile

from common.object_storage import get_object_storage


def get_patient(obj):
    """Resolve a Patient instance from various inputs (Patient, VoiceCaption with patient, legacy scanpair)."""
    if hasattr(obj, "_meta") and getattr(obj._meta, "model_name", "") == "patient":
        return obj
    if hasattr(obj, "patient") and getattr(obj, "patient") is not None:
        return getattr(obj, "patient")
    raise ValueError("Cannot resolve Patient from object")


def domain_for_patient(patient) -> str:
    from common.domains import normalize_domain

    app_label = getattr(getattr(patient, "_meta", None), "app_label", "")
    # app_label is the domain slug for every domain app (maxillo/brain/
    # laparoscopy); normalize_domain falls back to the default for anything else.
    return normalize_domain(app_label)


def entity_fk_kwargs(patient):
    """Patient-FK kwargs for FileRegistry/Job creation, keyed by the registry.

    Returns ``{"domain": ..., "<patient_fk>": patient, <others>: None}`` so a
    single call fills exactly one of the parallel patient FK columns.
    """
    from common.domains import DOMAIN_FK_FIELDS, normalize_domain

    domain = normalize_domain(domain_for_patient(patient))
    kwargs = {"domain": domain}
    for slug, (patient_fk, _voice_fk) in DOMAIN_FK_FIELDS.items():
        kwargs[patient_fk] = patient if slug == domain else None
    return kwargs


def create_step_jobs(source_job):
    """Spawn the downstream ProcessingStep pipeline for a just-created source job.

    The ``source_job`` stands in for its modality's *root* step (the step whose
    slug equals ``source_job.modality_slug``). Every enabled step reachable from
    that root via ``depends_on`` edges — including steps of *other* modalities
    that depend on it (e.g. bite_classification -> ios) — is created as a fresh
    ``dependency``-status Job wired to its prerequisites' jobs. Their inputs are
    filled from the prerequisites' outputs when they unblock
    (Job._pull_dependency_outputs).

    No-op (returns ``[]``) when the modality declares no matching root step, so
    modalities without a pipeline keep their historical single-job behavior.
    Domain-agnostic: reads the source job's own entity FKs.
    """
    from common.models import Job, ProcessingStep

    created = []
    if not source_job:
        return created
    modality_slug = str(getattr(source_job, "modality_slug", "") or "").strip()
    if not modality_slug:
        return created

    try:
        root_step = ProcessingStep.objects.filter(
            slug=modality_slug, is_enabled=True
        ).first()
        if root_step is None:
            return created
        all_steps = list(
            ProcessingStep.objects.filter(is_enabled=True).prefetch_related("depends_on")
        )
    except Exception:
        # Table not migrated yet, or a transient DB error: fall back to the
        # historical single-job behavior rather than break the upload.
        return created

    patient = source_job.get_patient()
    if patient is None:
        return created
    entity_kwargs = entity_fk_kwargs(patient)
    priority = getattr(source_job, "priority", 0) or 0

    # The source job already satisfies the root step. Grow the set of created
    # step-jobs to a fixpoint over the DAG: a step is instantiated once every
    # one of its prerequisites is represented by a job (rooted at source_job).
    job_by_step = {root_step.id: source_job}
    progressed = True
    while progressed:
        progressed = False
        for step in all_steps:
            if step.id in job_by_step:
                continue
            deps = list(step.depends_on.all())
            if not deps:
                continue  # a root step of some other modality; not triggered here
            if not all(d.id in job_by_step for d in deps):
                continue
            job = Job.objects.create(
                step=step,
                modality_slug=step.slug,
                status="dependency",
                input_files={},
                priority=priority,
                **entity_kwargs,
            )
            for d in deps:
                job.add_dependency(job_by_step[d.id])
            job_by_step[step.id] = job
            created.append(job)
            progressed = True
    return created


def project_slug_from_patient(patient) -> str:
    domain = domain_for_patient(patient)
    if domain == "laparoscopy":
        return "laparoscopy"
    return "maxillo"


def raw_key_prefix_for(patient, modality_slug: str) -> str:
    project_slug = project_slug_from_patient(patient)
    return f"{project_slug}/raw/{modality_slug}".strip("/")


def processed_key_prefix_for(patient, modality_slug: str) -> str:
    project_slug = project_slug_from_patient(patient)
    return f"{project_slug}/processed/{modality_slug}".strip("/")


def sanitize_relpath(p: str) -> str:
    p = (p or "").lstrip("/").replace("\\", "/")
    parts = [seg for seg in p.split("/") if seg and seg not in {".", ".."}]
    return "/".join(parts)


def upload_uploaded_file_to_storage(*, key: str, uploaded_file) -> tuple[str, int, str]:
    storage = get_object_storage()

    fd, tmp_path = tempfile.mkstemp(prefix="tf_upload_")
    os.close(fd)
    try:
        hash_sha256 = hashlib.sha256()
        size = 0
        with open(tmp_path, "wb+") as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)
                hash_sha256.update(chunk)
                size += len(chunk)

        storage.upload_file(tmp_path, key=key)
        return key, size, hash_sha256.hexdigest()
    finally:
        with contextlib.suppress(Exception):
            os.remove(tmp_path)
