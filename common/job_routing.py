import re
from typing import Any, Optional

from django.conf import settings

_QUEUE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _sanitize_queue_name(name: Optional[str], *, default: str) -> str:
    q = (name or "").strip()
    if not q:
        return default
    if not _QUEUE_NAME_RE.match(q):
        return default
    return q


def _project_slug_for_job(job: Any) -> Optional[str]:
    try:
        domain = getattr(job, "domain", "")

        if domain == "brain":
            patient = getattr(job, "brain_patient", None)
            if patient is None:
                voice_caption = getattr(job, "brain_voice_caption", None)
                patient = (
                    getattr(voice_caption, "patient", None)
                    if voice_caption is not None
                    else None
                )
            return "brain" if patient is not None else None

        if domain == "laparoscopy":
            patient = getattr(job, "laparoscopy_patient", None)
            if patient is None:
                voice_caption = getattr(job, "laparoscopy_voice_caption", None)
                patient = (
                    getattr(voice_caption, "patient", None)
                    if voice_caption is not None
                    else None
                )
            return "laparoscopy" if patient is not None else None
        if domain == "maxillo":
            patient = getattr(job, "patient", None)
            if patient is None:
                voice_caption = getattr(job, "voice_caption", None)
                patient = (
                    getattr(voice_caption, "patient", None)
                    if voice_caption is not None
                    else None
                )

            if patient is not None:
                project = getattr(patient, "project", None)
                slug = getattr(project, "slug", None) if project is not None else None
                if slug:
                    return str(slug)
                return "maxillo"
            
    except Exception:
        return None
    return None


def is_runner_enabled_for_modality(modality_slug: Optional[str]) -> bool:
    # Admin-driven config (Phase 4) with legacy env fallback when no row exists.
    from common.modality_config import modality_is_enabled
    return modality_is_enabled(modality_slug)


def select_runner_queue(job: Any) -> str:
    default_queue = getattr(settings, "RUNNER_DEFAULT_QUEUE", "runner") or "runner"
    queue_by_project = getattr(settings, "RUNNER_QUEUE_BY_PROJECT", None) or {}
    queue_by_modality = getattr(settings, "RUNNER_QUEUE_BY_MODALITY", None) or {}

    modality_slug = getattr(job, "modality_slug", None)

    # DB queue override wins over ALL env routing (maintainer decision).
    from common.modality_config import queue_override_for
    db_queue = queue_override_for(modality_slug)
    if db_queue:
        return _sanitize_queue_name(db_queue, default=default_queue)

    project_slug = _project_slug_for_job(job)
    if project_slug and isinstance(queue_by_project, dict):
        q = queue_by_project.get(project_slug)
        if isinstance(q, str) and q.strip():
            return _sanitize_queue_name(q, default=default_queue)

    if modality_slug and isinstance(queue_by_modality, dict):
        q = queue_by_modality.get(str(modality_slug))
        if isinstance(q, str) and q.strip():
            return _sanitize_queue_name(q, default=default_queue)

    return _sanitize_queue_name(default_queue, default="runner")
