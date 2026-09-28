"""Password recovery: Django's token-based reset flow, branded and guarded.

It was added in 2.x under ``toothfairy/urls.py`` and lost when the project
became ``yggdrasil`` in 3.0 (#96). It lives here rather than beside the rest of
auth in ``maxillo/views/auth.py`` because that address is legacy, not a pattern
(``maxillo/README.md``).

On top of Django's views:

* The email is the branded text + HTML one the invitations send
  (``common.emails``).
* The shared demo guest can never be reset: its account is created by hand, so
  nothing guarantees it has no email address, and whoever reset it would own
  the account every phone visitor is logged into.
* One address gets at most ``RESET_EMAILS_PER_WINDOW`` emails per window, so the
  form cannot be used to flood somebody's inbox. Over the limit the page still
  says "sent" -- it says that for unknown addresses too, so it never tells a
  visitor which addresses have accounts.
* A completed reset clears the django-axes lockout for that username: someone
  who forgot their password has often just locked themselves out guessing it.
"""
import hashlib
import logging

from axes.utils import reset as reset_axes_lockout
from django.conf import settings
from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import PasswordResetForm
from django.core.cache import cache
from django.http import HttpResponseRedirect
from django.template.loader import render_to_string
from django.urls import reverse_lazy

from common.demo import is_demo_guest
from common.emails import send_branded_email

logger = logging.getLogger(__name__)

RESET_EMAILS_PER_WINDOW = 3
RESET_WINDOW_SECONDS = 60 * 60


def reset_link_lifetime():
    """How long a reset link stays valid, in words, for the email."""
    seconds = settings.PASSWORD_RESET_TIMEOUT
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} day{'s' if days != 1 else ''}"
    hours = max(1, seconds // 3600)
    return f"{hours} hour{'s' if hours != 1 else ''}"


def _within_rate(email):
    """Count one reset request for ``email``; False once over the limit."""
    digest = hashlib.sha256(email.strip().lower().encode()).hexdigest()
    key = f"password-reset-rl:{digest}"
    try:
        # add() seeds the window with its TTL only on the first hit; incr keeps it.
        if cache.add(key, 1, RESET_WINDOW_SECONDS):
            return True
        return cache.incr(key) <= RESET_EMAILS_PER_WINDOW
    except Exception:
        # A cache hiccup must not stop people from recovering their accounts.
        return True


class PasswordResetRequestForm(PasswordResetForm):
    def get_users(self, email):
        return (user for user in super().get_users(email) if not is_demo_guest(user))

    def send_mail(
        self,
        subject_template_name,
        email_template_name,
        context,
        from_email,
        to_email,
        html_email_template_name=None,
    ):
        subject = "".join(render_to_string(subject_template_name, context).splitlines())
        try:
            send_branded_email(
                subject,
                render_to_string(email_template_name, context),
                render_to_string(html_email_template_name, context),
                from_email,
                [to_email],
            )
        except Exception:
            # Same as Django's own form: a mail failure must not become a 500, which
            # would also tell the visitor that the address has an account.
            logger.exception("Failed to send password reset email to user %s", context["user"].pk)


class PasswordResetRequestView(auth_views.PasswordResetView):
    template_name = "registration/password_reset_form.html"
    subject_template_name = "registration/emails/password_reset_subject.txt"
    email_template_name = "registration/emails/password_reset_body.txt"
    html_email_template_name = "registration/emails/password_reset_body.html"
    form_class = PasswordResetRequestForm
    success_url = reverse_lazy("password_reset_done")

    @property
    def extra_email_context(self):
        return {"link_lifetime": reset_link_lifetime()}

    def form_valid(self, form):
        if not _within_rate(form.cleaned_data["email"]):
            logger.warning("Password reset rate limit reached; no email sent")
            return HttpResponseRedirect(self.get_success_url())
        return super().form_valid(form)


class PasswordResetSentView(auth_views.PasswordResetDoneView):
    template_name = "registration/password_reset_done.html"


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "registration/password_reset_confirm.html"
    success_url = reverse_lazy("password_reset_complete")

    def form_valid(self, form):
        response = super().form_valid(form)
        reset_axes_lockout(username=form.user.get_username())
        return response


class PasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    template_name = "registration/password_reset_complete.html"
