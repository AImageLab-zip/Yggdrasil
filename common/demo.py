"""Anonymous public guest demo (Phase 7).

A read-only, no-login window onto *curated* folders — those an admin has
flagged ``is_demo=True`` (see ``common.base_models.FolderBase``). Everything
here is deliberately self-contained: it never touches the authenticated app
views or their ``@login_required`` file endpoints, and it never widens access
to anything outside a demo folder. The only data it can reach is a patient
that lives in an ``is_demo`` folder, and only via GET/HEAD.

Security invariants (keep these true):
  * Every queryset is rooted at ``is_demo=True`` folders.
  * A patient is reachable only if ``patient_in_demo(patient, domain)``.
  * A file is streamed only if its patient satisfies the same check.
  * No write path exists; the guard rejects non-GET/HEAD and rate-limits by IP.
"""

import functools
import mimetypes

from django.apps import apps
from django.core.cache import cache
from django.http import Http404, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, render

from .domains import DOMAINS, fk_fields_for
from .file_access import streaming_response
from .models import FileRegistry

# Per-IP fixed-window throttle. Uses the default cache (LocMemCache unless a
# shared cache is configured — good enough to blunt scraping; not a hard quota
# across gunicorn workers).
RATE_LIMIT = 120          # requests
RATE_WINDOW = 60          # seconds


def _client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _rate_ok(request):
    key = f"demo-rl:{_client_ip(request)}"
    try:
        count = cache.get(key, 0)
        if count >= RATE_LIMIT:
            return False
        # add() seeds the window with TTL only on first hit; incr keeps the TTL.
        if not cache.add(key, 1, RATE_WINDOW):
            cache.incr(key, 1)
    except Exception:
        # A cache hiccup must never take the demo down or, worse, fail open into
        # an error — just allow the request through.
        return True
    return True


def demo_guard(view):
    """GET/HEAD only + per-IP rate limit. No auth (that's the point)."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.method not in ("GET", "HEAD"):
            return HttpResponseNotAllowed(["GET", "HEAD"])
        if not _rate_ok(request):
            return HttpResponse("Too many requests", status=429)
        return view(request, *args, **kwargs)

    return wrapper


# --- querysets ------------------------------------------------------------

def _model(domain, name):
    return apps.get_model(domain, name)


def _patient_uses_m2m_folders(patient_model):
    return any(f.name == "folders" for f in patient_model._meta.get_fields())


def demo_folders(domain):
    """All ``is_demo`` folders for ``domain`` (empty if unknown domain)."""
    if domain not in DOMAINS:
        return None
    return _model(domain, "Folder").objects.filter(is_demo=True)


def demo_patients(domain):
    """Active patients that live in at least one ``is_demo`` folder."""
    Patient = _model(domain, "Patient")
    qs = Patient.objects.all()
    if _patient_uses_m2m_folders(Patient):
        qs = qs.filter(folders__is_demo=True).distinct()
    else:
        qs = qs.filter(folder__is_demo=True)
    # Exclude soft-deleted where the field exists (maxillo Patient.deleted).
    if any(f.name == "deleted" for f in Patient._meta.get_fields()):
        qs = qs.filter(deleted=False)
    return qs


def patient_in_demo(patient, domain):
    """True iff ``patient`` is exposed by a demo folder in ``domain``."""
    if patient is None or domain not in DOMAINS:
        return False
    if getattr(patient, "deleted", False):
        return False
    Patient = _model(domain, "Patient")
    if _patient_uses_m2m_folders(Patient):
        return patient.folders.filter(is_demo=True).exists()
    folder = getattr(patient, "folder", None)
    return bool(folder and folder.is_demo)


def _domains_with_demos():
    out = []
    for slug in DOMAINS:
        folders = demo_folders(slug)
        if folders is not None and folders.exists():
            out.append(slug)
    return out


def landing_demo_url():
    """Reverse of the demo index, but only when at least one demo folder exists
    (so the landing CTA stays hidden until content is published). ``None`` if
    nothing to show or the URLconf isn't ready."""
    from django.urls import reverse

    try:
        return reverse("demo:index") if _domains_with_demos() else None
    except Exception:
        return None


# --- views ----------------------------------------------------------------

@demo_guard
def demo_index(request):
    domains = []
    for slug in _domains_with_demos():
        domains.append({
            "slug": slug,
            "label": slug.capitalize(),
            "patient_count": demo_patients(slug).count(),
        })
    return render(request, "common/demo/index.html", {
        "demo_mode": True,
        "domains": domains,
    })


@demo_guard
def demo_domain_list(request, domain):
    if domain not in DOMAINS or not demo_folders(domain).exists():
        raise Http404("No demo for this domain")
    patients = demo_patients(domain).order_by("-uploaded_at")[:200]
    return render(request, "common/demo/list.html", {
        "demo_mode": True,
        "domain": domain,
        "domain_label": domain.capitalize(),
        "patients": patients,
    })


@demo_guard
def demo_patient_detail(request, domain, pk):
    if domain not in DOMAINS:
        raise Http404("Unknown domain")
    patient = get_object_or_404(demo_patients(domain), pk=pk)
    fk = fk_fields_for(domain)[0]
    files = (
        FileRegistry.objects.filter(**{fk: patient})
        .order_by("file_type", "id")
    )
    file_rows = []
    for f in files:
        ct, _ = mimetypes.guess_type(f.file_path or "")
        file_rows.append({
            "id": f.id,
            "file_type": f.get_file_type_display() if hasattr(f, "get_file_type_display") else f.file_type,
            "content_type": ct or "application/octet-stream",
            "is_image": bool(ct and ct.startswith("image/")),
            "is_video": bool(ct and ct.startswith("video/")),
            "size": f.file_size or 0,
        })
    return render(request, "common/demo/detail.html", {
        "demo_mode": True,
        "domain": domain,
        "domain_label": domain.capitalize(),
        "patient": patient,
        "files": file_rows,
    })


@demo_guard
def demo_serve_file(request, domain, file_id):
    """Stream a single FileRegistry entry, but only if its patient is in a
    demo folder of ``domain``. Read-only, inline."""
    if domain not in DOMAINS:
        raise Http404("Unknown domain")
    try:
        file_obj = FileRegistry.objects.get(id=file_id)
    except FileRegistry.DoesNotExist:
        raise Http404("File not found")

    # Resolve the patient via the URL domain's FK (not file_obj.domain, which is
    # legacy/untrusted) so a file can only be reached through its own domain.
    patient = getattr(file_obj, fk_fields_for(domain)[0], None)
    if not patient_in_demo(patient, domain):
        # Do not leak existence — same answer as a missing file.
        raise Http404("File not found")

    path = file_obj.file_path
    if not path:
        raise Http404("File not found")

    content_type, _ = mimetypes.guess_type(path)
    content_type = content_type or "application/octet-stream"
    filename = str(path).split("/")[-1] or f"file_{file_obj.id}"

    resp = streaming_response(
        path_or_key=path,
        content_type=content_type,
        filename=filename,
        as_attachment=False,
    )
    if content_type.startswith(("video/", "audio/")):
        resp["Accept-Ranges"] = "bytes"
        if file_obj.file_size:
            resp["Content-Length"] = str(file_obj.file_size)
    return resp
