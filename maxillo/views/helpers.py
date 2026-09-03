"""Helper utilities for views.

`render_with_fallback` / `redirect_with_namespace` now live in
`common/view_helpers.py` (brain shared inferior copies of them) and are
re-exported here so existing import paths keep working.
"""
from django.urls import NoReverseMatch

from common.view_helpers import redirect_with_namespace, render_with_fallback

__all__ = ["render_with_fallback", "redirect_with_namespace", "bulk_upload_url_for"]


def bulk_upload_url_for(request, namespace: str):
    """URL of the bulk-upload screen, or None when it is out of reach.

    Bulk ingestion is administrators-only, needs a selected project, and is not
    routed in every domain (brain has no such view), so resolve it here and let
    templates render the entry point with a plain ``{% if bulk_upload_url %}``.
    """
    from django.urls import reverse

    from common.permissions import user_is_project_admin

    if not request.session.get('current_project_id'):
        return None
    if not user_is_project_admin(request.user, request):
        return None
    profile = getattr(request.user, 'profile', None)
    if not (profile and profile.can_upload_scans()):
        return None
    try:
        return reverse(f'{namespace}:bulk_upload_patients')
    except NoReverseMatch:
        return None
