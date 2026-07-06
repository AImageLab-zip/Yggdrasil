"""Central accessors for admin-driven processing config (Phase 4).

Per-modality processing is now declared as ``ProcessingStep`` rows (a modality
can have several, forming a DAG). Every reader is keyed by a **step slug**,
which is what ``Job.modality_slug`` carries: a modality's *root* step has
``slug == modality.slug``, so passing a plain modality slug resolves that root
step, while a downstream step (e.g. ``ios_orientation``) resolves itself.

When no step row exists for a slug (e.g. reads before the table is migrated, or
an ad-hoc modality with no pipeline), readers fall back to the historical
hardcoded/env behavior, so rollout stays zero-risk.

This module holds the single source of truth for the enablement rule so that
``common.job_routing`` can delegate here without circular recursion.
"""
import logging

from django.conf import settings
from django.db.utils import DatabaseError

logger = logging.getLogger(__name__)

# Historical hardcoded list from maxillo/file_utils.py: image modalities whose
# upload Job needs no runner processing.
_LEGACY_NO_PROCESSING = {"panoramic", "teleradiography", "rawzip"}


def _env_is_enabled(modality_slug):
    """Current env behavior of is_runner_enabled_for_modality.

    Empty/absent RUNNER_QUEUE_BY_MODALITY => everything enabled; otherwise a
    modality is enabled only when it has a non-blank queue entry.
    """
    queue_by_modality = getattr(settings, "RUNNER_QUEUE_BY_MODALITY", None) or {}
    if not isinstance(queue_by_modality, dict) or not queue_by_modality:
        return True
    slug = str(modality_slug or "").strip()
    if not slug:
        return False
    queue = queue_by_modality.get(slug)
    return isinstance(queue, str) and bool(queue.strip())


def get_step(slug):
    """Return the ProcessingStep whose slug matches, or None if absent.

    Swallows DatabaseError (e.g. reads before the table is migrated) so callers
    fall back to legacy behavior.
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

    True iff its step exists and is enabled; when no step row exists, fall back
    to the historical no-processing list.
    """
    step = get_step(modality_slug)
    if step is not None:
        return step.is_enabled
    return str(modality_slug or "").strip() not in _LEGACY_NO_PROCESSING


def modality_is_enabled(modality_slug):
    """Whether the runner is enabled for this step slug (DB config or env)."""
    step = get_step(modality_slug)
    if step is not None:
        return step.is_enabled
    return _env_is_enabled(modality_slug)


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


def modality_discard_raw(modality_slug):
    """Whether this modality's raw input files are hidden as a security screen.

    True only when a step row exists and has ``discard_raw`` set; absent a step
    row we stay permissive (legacy behavior)."""
    step = get_step(modality_slug)
    if step is not None:
        return bool(step.discard_raw)
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
    if file_type.endswith("_raw"):
        return file_type[: -len("_raw")]
    return ""


def _file_is_raw(file_obj):
    file_type = str(getattr(file_obj, "file_type", "") or "")
    return file_type.endswith("_raw") or file_type == "rgb_image"


def _processed_exists_for(file_obj, slug):
    """Whether a ``{slug}_processed`` file exists for the same owning patient."""
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
            file_type=f"{slug}_processed", **filters
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
