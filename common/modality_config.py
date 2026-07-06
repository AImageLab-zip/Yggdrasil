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
