"""Helpers for the 2.0 activity feed, recently-viewed strip, and notifications.

All three are additive and best-effort: a failure here must never break the
surrounding request (upload, view, caption, …), so every writer swallows
exceptions and logs. Import lazily where used to avoid app-loading cycles.
"""

import logging

logger = logging.getLogger(__name__)


def log_activity(actor, domain, patient_pk, patient_name="", verb="", target="", **metadata):
    """Append one ActivityEvent (patient-view Activity tab / audit feed)."""
    try:
        from common.models import ActivityEvent
        ActivityEvent.objects.create(
            actor=actor if getattr(actor, "is_authenticated", False) else None,
            domain=str(domain or ""),
            patient_pk=patient_pk,
            patient_name=str(patient_name or "")[:255],
            verb=str(verb or "")[:40],
            target=str(target or "")[:255],
            metadata=metadata or {},
        )
    except Exception:  # noqa: BLE001
        logger.exception("log_activity failed (%s %s #%s)", verb, domain, patient_pk)


def record_recent(user, domain, patient_pk, patient_name="", project_label="", icon=""):
    """Upsert a RecentlyViewed row and cap the list at 10 per user."""
    if not getattr(user, "is_authenticated", False):
        return
    try:
        from common.models import RecentlyViewed, SiteMaintenance
        maintenance = SiteMaintenance.get_solo()
        if maintenance.access_mode != SiteMaintenance.MODE_NORMAL and not user.is_staff:
            return
        RecentlyViewed.objects.update_or_create(
            user=user, domain=str(domain or ""), patient_pk=patient_pk,
            defaults={
                "patient_name": str(patient_name or "")[:255],
                "project_label": str(project_label or "")[:120],
                "icon": str(icon or "")[:40],
            },
        )
        # Trim to the 10 most recent for this user.
        stale = list(
            RecentlyViewed.objects.filter(user=user)
            .order_by("-viewed_at")
            .values_list("id", flat=True)[10:]
        )
        if stale:
            RecentlyViewed.objects.filter(id__in=stale).delete()
    except Exception:  # noqa: BLE001
        logger.exception("record_recent failed (%s #%s)", domain, patient_pk)


def notify(user, message, level="info", url=""):
    """Create an in-app Notification (topbar bell)."""
    if not getattr(user, "is_authenticated", False):
        return
    try:
        from common.models import Notification
        Notification.objects.create(
            user=user, level=level, message=str(message or "")[:500], url=str(url or "")[:500]
        )
    except Exception:  # noqa: BLE001
        logger.exception("notify failed for %s", getattr(user, "id", "?"))
