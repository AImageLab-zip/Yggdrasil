"""Phones get the public demo and nothing else.

Two signals, two jobs:

  * The **server** decides auth policy from the request: a phone may not sign in
    or register. ``is_mobile_request`` reads the ``Sec-CH-UA-Mobile`` client hint
    when the browser sends one and falls back to the User-Agent.
  * **CSS width** decides layout (``theme.css``), and is also the fallback that
    hides the sign-in UI on a narrow screen the server did not recognise.

Any link opens in the demo on a phone: ``@login_required`` sends an anonymous
visitor to ``/login/?next=...``, and ``MobileAwareLoginView`` lets a phone into
the guest account there (``common.demo.login_as_guest``) and on to ``next``.

Tablets are deliberately *not* mobile: iPadOS sends a desktop User-Agent and
Android tablets omit the ``Mobile`` token, so both keep the full site.

This is a product policy, not a security boundary. A phone set to "request
desktop site" gets through, and that is accepted: the guard exists so the
phone UI never offers something it cannot lay out, not to keep anyone out.
"""

import functools
import re

from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth import views as auth_views
from django.shortcuts import redirect, render, resolve_url
from django.utils.cache import patch_vary_headers
from django.utils.http import url_has_allowed_host_and_scheme

# Phones only. ``Mobi`` covers iOS Safari/Chrome and most Android browsers;
# ``Android.*Mobile`` is spelled out for the older Android stock browser.
_MOBILE_UA = re.compile(r"Mobi|iPhone|iPod|Android.*Mobile", re.IGNORECASE)
# iPads up to iOS 12 still say ``Mobile/15E148``; a tablet keeps the full site.
_TABLET_UA = re.compile(r"iPad", re.IGNORECASE)

MOBILE_VARY = ("User-Agent", "Sec-CH-UA-Mobile")

LOGIN_MOBILE_TEMPLATE = "registration/login_mobile.html"


def is_mobile_request(request):
    """True iff ``request`` comes from a phone (tablets and desktops are False)."""
    if request is None:
        return False
    hint = request.META.get("HTTP_SEC_CH_UA_MOBILE")
    if hint is not None:
        # Structured-header boolean: ``?1`` is true, ``?0`` false. A browser
        # that sends the hint knows better than our regex does.
        return hint.strip() == "?1"
    ua = request.META.get("HTTP_USER_AGENT", "")
    return bool(_MOBILE_UA.search(ua)) and not _TABLET_UA.search(ua)


def mark_mobile_varying(response):
    """The response differs by device: tell caches, and ask for the client hint."""
    patch_vary_headers(response, MOBILE_VARY)
    response["Accept-CH"] = "Sec-CH-UA-Mobile"
    return response


def render_desktop_only(request):
    """The demo-only card phones get instead of a sign-in or register form.

    GET renders it with 200. Anything else (a form POST) gets the same page with
    403 and never reaches ``authenticate``, so django-axes does not count it.
    """
    status = 200 if request.method in ("GET", "HEAD") else 403
    response = render(request, LOGIN_MOBILE_TEMPLATE, status=status)
    return mark_mobile_varying(response)


def desktop_auth_only(view):
    """Serve the demo-only card to phones instead of running ``view``."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if is_mobile_request(request):
            return render_desktop_only(request)
        return mark_mobile_varying(view(request, *args, **kwargs))

    return wrapper


class MobileAwareLoginView(auth_views.LoginView):
    """Django's ``LoginView``, except that a phone never gets the form.

    Every ``@login_required`` page sends an anonymous visitor here with
    ``?next=<the link they opened>``. A phone cannot sign in, so rather than a
    dead end it is let into the public demo and sent on to that link: any link
    to the platform opens straight in the demo. Only when there is no demo to
    enter does it get the demo-only card, which then says so.
    """

    def dispatch(self, request, *args, **kwargs):
        if is_mobile_request(request):
            return self._phone(request)
        return mark_mobile_varying(super().dispatch(request, *args, **kwargs))

    def _phone(self, request):
        from common.demo import _rate_ok, login_as_guest

        if (
            request.method in ("GET", "HEAD")
            and not request.user.is_authenticated
            and _rate_ok(request)
            and login_as_guest(request) is not None
        ):
            return mark_mobile_varying(redirect(self._safe_next(request)))
        return render_desktop_only(request)

    def _safe_next(self, request):
        """``next`` if it stays on this site, else the home page."""
        target = request.GET.get(REDIRECT_FIELD_NAME, "")
        if target and url_has_allowed_host_and_scheme(
            target,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return target
        return resolve_url("home")
