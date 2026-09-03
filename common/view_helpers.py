"""Namespace-aware view helpers shared by every domain app.

`render_with_fallback` and `redirect_with_namespace` used to be duplicated in
`maxillo/views/helpers.py` and `brain/helpers.py`; brain's copies hardcoded the
namespace instead of reading it off `request.resolver_match`. Both modules now
re-export these, so call sites keep importing from wherever they already did.
"""
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
