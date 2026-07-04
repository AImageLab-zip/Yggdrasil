"""Central accessors for admin-driven per-modality processing config (Phase 4).

All readers fall back to the historical hardcoded/env behavior when no
``ModalityProcessingConfig`` row exists for a modality (or the table is not yet
migrated), so the feature rolls out with zero behavior change: a data migration
seeds one row per existing modality reproducing current behavior.

This module holds the single source of truth for the env-based enablement rule
so that ``common.job_routing`` can delegate here without circular recursion.
"""
import logging

from django.conf import settings
from django.db.utils import DatabaseError

logger = logging.getLogger(__name__)

# Historical hardcoded list from maxillo/file_utils.py: image modalities whose
# upload Job is born 'completed' (no runner processing).
_LEGACY_NO_PROCESSING = {"panoramic", "teleradiography", "rawzip"}

# Historical hardcoded dependency wired imperatively at IOS upload time:
# a bite_classification job depends on the ios job.
_LEGACY_DEPENDENTS = {"ios": ["bite_classification"]}


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


def get_processing_config(modality_slug):
    """Return the ModalityProcessingConfig for a slug, or None if absent.

    Swallows DatabaseError (e.g. reads before the table is migrated) so callers
    fall back to legacy behavior.
    """
    slug = str(modality_slug or "").strip()
    if not slug:
        return None
    from common.models import ModalityProcessingConfig
    try:
        return (
            ModalityProcessingConfig.objects.select_related("modality")
            .filter(modality__slug=slug)
            .first()
        )
    except DatabaseError:
        logger.warning("ModalityProcessingConfig lookup failed for '%s'; using legacy fallback", slug)
        return None


def modality_requires_processing(modality_slug):
    """Whether an upload for this modality needs runner processing.

    Config value if a row exists, else the legacy hardcoded list.
    """
    config = get_processing_config(modality_slug)
    if config is not None:
        return config.requires_processing
    return str(modality_slug or "").strip() not in _LEGACY_NO_PROCESSING


def modality_is_enabled(modality_slug):
    """Whether the runner is enabled for this modality (DB config or env)."""
    config = get_processing_config(modality_slug)
    if config is not None:
        return config.is_enabled
    return _env_is_enabled(modality_slug)


def queue_override_for(modality_slug):
    """DB queue override for this modality, or None when unset.

    A non-blank value wins over ALL env routing (maintainer decision).
    """
    config = get_processing_config(modality_slug)
    if config is not None and config.queue_name and config.queue_name.strip():
        return config.queue_name.strip()
    return None


def modality_is_blocking(modality_slug):
    """Whether an in-flight job gates patient readiness (shows 'processing')."""
    config = get_processing_config(modality_slug)
    if config is not None:
        return config.is_blocking
    return modality_requires_processing(modality_slug)


def dependent_slugs_of(modality_slug):
    """Slugs of enabled modalities whose config depends_on this modality.

    Used to generalize the ios -> bite_classification dependency: after a source
    job is created, dependent jobs are created for each returned slug.
    """
    slug = str(modality_slug or "").strip()
    if not slug:
        return []
    from common.models import ModalityProcessingConfig
    try:
        configs = (
            ModalityProcessingConfig.objects.filter(
                depends_on__slug=slug, is_enabled=True
            )
            .select_related("modality")
            .distinct()
        )
        found = [c.modality.slug for c in configs]
    except DatabaseError:
        logger.warning("dependent_slugs_of failed for '%s'; using legacy fallback", slug)
        found = None

    if found:
        return found
    # Legacy fallback (absent-row rule): apply the historical dependency only for
    # dependents that have no config row of their own, so an admin who cleared a
    # dependent's depends_on is respected.
    legacy = _LEGACY_DEPENDENTS.get(slug, [])
    return [dep for dep in legacy if get_processing_config(dep) is None]
