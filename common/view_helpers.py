"""Namespace-aware view helpers shared by every domain app.

`render_with_fallback` and `redirect_with_namespace` used to be duplicated in
`maxillo/views/helpers.py` and `brain/helpers.py`; brain's copies hardcoded the
namespace instead of reading it off `request.resolver_match`. Both modules now
re-export these, so call sites keep importing from wherever they already did.
"""
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.template.loader import select_template
from django.urls import NoReverseMatch, reverse


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


def wants_json(request):
    """True when the request came from the XHR uploader rather than a form post.

    `static/js/cbct_upload.js` sets this header and expects a JSON body; a plain
    form post of the same view expects messages + a redirect.
    """
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def patient_list_response(request):
    """The success response for an upload, in whichever shape the caller wants.

    The XHR uploader navigates on `{"ok": true, "redirect": ...}` and treats any
    other body as a failure -- including the HTML it gets by transparently
    following a 302, which is what made a successful brain upload report
    "Upload failed (HTTP 200)". Both domains go through here so the contract
    cannot drift again.
    """
    if not wants_json(request):
        return redirect_with_namespace(request, 'patient_list')
    ns = (getattr(request, 'resolver_match', None) and request.resolver_match.namespace) or 'maxillo'
    try:
        redirect_url = reverse(f"{ns}:patient_list")
    except NoReverseMatch:
        redirect_url = reverse('maxillo:patient_list')
    return JsonResponse({'ok': True, 'redirect': redirect_url})


def upload_error_response(request, message, status=400):
    """The failure counterpart of `patient_list_response`, or None for form posts.

    Returns the JSON the uploader turns into its toast; returns None when the
    caller should fall back to re-rendering the form with `messages`.
    """
    if not wants_json(request):
        return None
    return JsonResponse({'ok': False, 'error': message}, status=status)
