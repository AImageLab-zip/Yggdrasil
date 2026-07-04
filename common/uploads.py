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
    app_label = getattr(getattr(patient, "_meta", None), "app_label", "")
    if app_label == "laparoscopy":
        return "laparoscopy"
    return "maxillo"


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
