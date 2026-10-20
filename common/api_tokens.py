"""Personal API tokens: how a script calls ``/api/`` without a browser.

A client sends ``Authorization: Bearer ygg_…``. :class:`ApiTokenMiddleware`
resolves the token to its user before any view runs, so every permission check
downstream -- and every middleware after it (maintenance, demo read-only) -- sees
the real user, exactly as if they had logged in. Nothing else changes: a token
grants what its user may do, no more.

* **Only ``/api/``.** The HTML pages stay session-only. ``/api/runner/`` is
  skipped altogether: it has its own bearer tokens (``RUNNER_API_TOKENS``) and a
  frozen contract.
* **No CSRF check for a token request.** CSRF exists because a browser attaches
  cookies to a forged request on its own; it never attaches an ``Authorization``
  header, so a request that carries a valid token was written by its holder.
  A request *without* the header keeps full CSRF protection.
* **No session.** The token authenticates one request; no cookie is set.
* **A failed token is refused, never ignored.** Falling back to the session
  would make a typo'd token silently act as whoever is logged in.
* **Guessing is throttled per address.** After ``FAILURE_LIMIT`` bad tokens in
  ``FAILURE_WINDOW`` seconds the address gets 429 without a lookup. The address is
  resolved the way django-axes resolves it for logins, so the two agree.
"""
import logging
from datetime import timedelta

from axes.helpers import get_client_ip_address
from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin

from common.demo import is_demo_guest
from common.models import ApiToken

logger = logging.getLogger(__name__)

FAILURE_LIMIT = 20
FAILURE_WINDOW = 15 * 60  # seconds

#: ``last_used_at`` is an audit hint, not a log: write it at most this often.
LAST_USED_RESOLUTION = timedelta(minutes=5)


def issue_token(user, name, *, created_by=None, expires_at=None):
    """Create a token for ``user``; returns ``(token, plaintext_key)``.

    The plaintext exists only in the return value -- show it once and drop it.
    The shared demo guest can never hold one: everyone on the internet is logged
    in as that account, and a token would outlive its read-only middleware check
    on whatever the token was later used for.
    """
    if is_demo_guest(user):
        raise ValueError("The public demo account cannot hold an API token.")
    if not user.is_active:
        raise ValueError("An inactive user cannot hold an API token.")
    key = ApiToken.new_key()
    token = ApiToken(user=user, name=name, created_by=created_by, expires_at=expires_at)
    token.set_key(key)
    token.save()
    return token, key


def _bearer(request):
    """The bearer credential, ``""`` for an empty one, None for no bearer header."""
    header = request.META.get("HTTP_AUTHORIZATION", "")
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return credential.strip()


def _failure_key(request):
    return f"api-token-fail:{get_client_ip_address(request) or 'unknown'}"


def _too_many_failures(request):
    return (cache.get(_failure_key(request)) or 0) >= FAILURE_LIMIT


def _record_failure(request):
    key = _failure_key(request)
    if cache.add(key, 1, FAILURE_WINDOW):
        return
    try:
        cache.incr(key)
    except ValueError:  # expired between add() and incr()
        cache.add(key, 1, FAILURE_WINDOW)


def _resolve(key):
    if not key.startswith(ApiToken.KEY_PREFIX):
        return None
    token = (
        ApiToken.objects.select_related("user")
        .filter(key_hash=ApiToken.hash_key(key))
        .first()
    )
    if token is None or not token.is_usable() or is_demo_guest(token.user):
        return None
    return token


class ApiTokenMiddleware(MiddlewareMixin):
    """Authenticate ``/api/`` requests that carry a personal token.

    Must sit after ``AuthenticationMiddleware`` (it replaces ``request.user``)
    and before everything that reads the user.
    """

    def process_request(self, request):
        if not request.path.startswith("/api/") or "/api/runner/" in request.path:
            return None
        key = _bearer(request)
        if key is None:
            return None

        if _too_many_failures(request):
            return JsonResponse(
                {"error": "Too many invalid API tokens from this address. Try again later."},
                status=429,
            )
        token = _resolve(key)
        if token is None:
            _record_failure(request)
            logger.warning("Refused API token from %s", get_client_ip_address(request))
            return JsonResponse({"error": "Invalid or expired API token"}, status=401)

        user = token.user
        request.user = user

        async def auser():
            return user

        request.auser = auser
        request.api_token = token
        request._dont_enforce_csrf_checks = True

        now = timezone.now()
        if token.last_used_at is None or now - token.last_used_at > LAST_USED_RESOLUTION:
            ApiToken.objects.filter(pk=token.pk).update(last_used_at=now)
        return None
