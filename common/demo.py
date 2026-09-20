"""Public guest demo.

Guests explore the *real* portal — the same interactive viewer logged-in users
see — but strictly read-only. Rather than a bespoke UI, ``demo_index`` logs the
visitor in as a shared low-privilege guest user and drops them on the ordinary
domain chooser; the existing ``@login_required`` views then work unchanged.

What the guest may see is decided by ``ProjectAccess`` alone, exactly as for any
other user: a project is in the public demo iff the guest holds ``viewer`` on
it. This used to be a separate ``Folder.is_demo`` flag that *bypassed*
``ProjectAccess`` for the guest, which meant granting the guest a role did
nothing and the real control was invisible on the folder.

Security invariants (keep these true):
  * The guest's role must never be more than ``viewer``. Granting it publishes a
    project to anyone on the internet, since ``/demo/`` needs no password.
  * Every write is a non-safe HTTP method and is rejected for the guest by
    ``DemoGuestReadOnlyMiddleware`` (logout excepted).
"""

import functools

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.core.cache import cache
from django.http import Http404, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import redirect

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


def demo_is_published():
    """True iff the guest account can actually show a visitor something.

    That is: the guest exists and holds a readable role on some active project.
    Keeps the landing CTA hidden until an admin has published something.
    """
    from common.models import ProjectAccess
    from common.permissions import READ_ROLES

    username = getattr(settings, "DEMO_GUEST_USERNAME", None)
    if not username:
        return False
    return ProjectAccess.objects.filter(
        user__username=username,
        user__is_active=True,
        project__is_active=True,
        role__in=READ_ROLES,
    ).exists()


def demo_domain_cards():
    """The domains the demo actually covers, for the landing CTA's chips.

    Built from the guest's own access through ``landing_domain_cards``, so the
    CTA advertises exactly what the visitor will find after clicking -- not a
    fixed list of three.
    """
    from common.domains import landing_domain_cards

    username = getattr(settings, "DEMO_GUEST_USERNAME", None)
    if not username:
        return []
    guest = get_user_model().objects.filter(username=username, is_active=True).first()
    if guest is None:
        return []
    return landing_domain_cards(guest)


def landing_demo_url():
    """Reverse of the demo index, or ``None`` when there is nothing to show (or
    the URLconf isn't ready)."""
    from django.urls import reverse

    try:
        return reverse("demo:index") if demo_is_published() else None
    except Exception:
        return None


# --- views ----------------------------------------------------------------

def demo_index(request):
    """Public entry point. Logs the visitor in as the shared read-only guest
    user, then hands them the ordinary domain chooser so they can pick where to
    start -- the demo may span several domains, and picking one for them hid the
    rest. GET/HEAD only; keeps the per-IP throttle to blunt session-spam."""
    if request.method not in ("GET", "HEAD"):
        return HttpResponseNotAllowed(["GET", "HEAD"])
    if not _rate_ok(request):
        return HttpResponse("Too many requests", status=429)

    if not demo_is_published():
        raise Http404("No demo content is available")

    User = get_user_model()
    try:
        guest = User.objects.get(username=settings.DEMO_GUEST_USERNAME)
    except User.DoesNotExist:
        raise Http404("Demo is not available")

    login(request, guest, backend="django.contrib.auth.backends.ModelBackend")
    return redirect("home")
