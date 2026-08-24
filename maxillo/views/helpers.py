"""Helper utilities for views."""
from django.shortcuts import render, redirect
from django.template.loader import select_template
from django.urls import NoReverseMatch


def render_with_fallback(request, base_template_name: str, context: dict):
    """Render a template preferring app-specific, then common templates.

    base_template_name: e.g., 'patient_list', 'patient_detail'
    Resolves to one of:
      - f"{ns}/{base_template_name}.html"
      - f"common/{base_template_name}.html"
    """
    ns = (request.resolver_match.namespace or '').strip() or 'maxillo'
    candidates = [
        f"{ns}/{base_template_name}.html",
        f"common/{base_template_name}.html",
    ]
    template = select_template(candidates)
    return render(request, template.template.name, context)


def redirect_with_namespace(request, name: str, *args, **kwargs):
    """Redirect using current namespace if present, otherwise fallback to global name.

    Example: redirect_with_namespace(request, 'patient_list') -> 'maxillo:patient_list' or 'patient_list'
    """
    ns = (getattr(request, 'resolver_match', None) and request.resolver_match.namespace) or ''
    if ns:
        try:
            return redirect(f"{ns}:{name}", *args, **kwargs)
        except NoReverseMatch:
            pass
    try:
        return redirect(name, *args, **kwargs)
    except NoReverseMatch:
        # Last resort: try maxillo namespace
        try:
            return redirect(f"maxillo:{name}", *args, **kwargs)
        except NoReverseMatch:
            return redirect('/')


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
