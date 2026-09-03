"""Share-link expiry rules for exports, shared by all domains.

null expires_at = never expires (pre-2.0 behavior for existing shares).
New/updated shares default to 30 days; "never" is reserved for staff and
project admins.
"""

from datetime import timedelta

from django.utils import timezone

SHARE_EXPIRY_DEFAULT_DAYS = 30
SHARE_EXPIRY_MAX_DAYS = 365


def is_share_expired(export):
    expires_at = getattr(export, "expires_at", None)
    return bool(expires_at) and timezone.now() >= expires_at


def resolve_share_expiry(raw, *, current, can_set_never):
    """Resolve the requested expiry for a share update.

    raw: value of "expires_in_days" from the payload - int-like, "never",
         or None/"" (absent).
    current: the export's current expires_at.
    Returns (expires_at, error): error is a message string when the request
    is invalid or not permitted, otherwise None.
    """
    if raw is None or raw == "":
        # Absent: keep an existing expiry, otherwise apply the default.
        if current is not None:
            return current, None
        return timezone.now() + timedelta(days=SHARE_EXPIRY_DEFAULT_DAYS), None

    if isinstance(raw, str) and raw.strip().lower() == "never":
        if not can_set_never:
            return None, "Only staff or project admins can create non-expiring links"
        return None, None

    try:
        days = int(raw)
    except (TypeError, ValueError):
        return None, "expires_in_days must be a number of days or 'never'"

    if not 1 <= days <= SHARE_EXPIRY_MAX_DAYS:
        return None, f"expires_in_days must be between 1 and {SHARE_EXPIRY_MAX_DAYS}"

    return timezone.now() + timedelta(days=days), None
