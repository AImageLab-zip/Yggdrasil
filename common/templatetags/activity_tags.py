"""Template access to the ActivityEvent feed (patient-view Activity panel)."""

from django import template

register = template.Library()


@register.simple_tag
def patient_activity(domain, patient_pk, limit=20):
    """Return the most recent ActivityEvents for a patient (best-effort)."""
    try:
        from common.models import ActivityEvent
        return list(
            ActivityEvent.objects.filter(domain=domain, patient_pk=patient_pk)[: int(limit)]
        )
    except Exception:  # noqa: BLE001
        return []


@register.simple_tag
def user_recents(user, limit=6):
    """Return the user's recently-viewed patients (landing 'continue') strip."""
    if not getattr(user, "is_authenticated", False):
        return []
    try:
        from common.models import RecentlyViewed
        return list(RecentlyViewed.objects.filter(user=user)[: int(limit)])
    except Exception:  # noqa: BLE001
        return []
