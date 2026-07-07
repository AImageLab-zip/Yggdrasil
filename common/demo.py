"""Public guest demo (Phase 7).

Guests explore the *real* portal — the same interactive viewer logged-in users
see — but strictly read-only and scoped to *curated* folders an admin flagged
``is_demo=True`` (see ``common.base_models.FolderBase``). Rather than a bespoke
UI, ``demo_index`` logs the visitor in as a shared low-privilege guest user and
redirects into the real app; the existing ``@login_required`` views then work
unchanged, with access narrowed by ``common.permissions`` (guest reads only
``is_demo`` folders) and writes blocked by ``DemoGuestReadOnlyMiddleware``.

Security invariants (keep these true):
  * The guest holds a ``standard`` ProjectAccess and no FolderAccess, so
    ``user_can_read_folder`` grants it only ``is_demo`` folders.
  * The guest role must never be ``admin`` (that would bypass is_demo scoping).
  * Every write is a non-safe HTTP method and is rejected for the guest by the
    read-only middleware (logout excepted).
"""

import functools

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.core.cache import cache
from django.http import Http404, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import redirect

from .domains import DOMAINS

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


def is_demo_guest(user):
    """True iff ``user`` is the shared read-only public-demo guest account."""
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and user.get_username() == getattr(settings, "DEMO_GUEST_USERNAME", None)
    )


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

def demo_index(request):
    """Public entry point. Logs the visitor in as the shared read-only guest
    user, then redirects into the real portal at the first demo-enabled domain.
    GET/HEAD only; keeps the per-IP throttle to blunt session-spam."""
    if request.method not in ("GET", "HEAD"):
        return HttpResponseNotAllowed(["GET", "HEAD"])
    if not _rate_ok(request):
        return HttpResponse("Too many requests", status=429)

    domains = _domains_with_demos()
    if not domains:
        raise Http404("No demo content is available")

    User = get_user_model()
    try:
        guest = User.objects.get(username=settings.DEMO_GUEST_USERNAME)
    except User.DoesNotExist:
        raise Http404("Demo is not available")

    login(request, guest, backend="django.contrib.auth.backends.ModelBackend")
    return redirect(f"/{domains[0]}/")
