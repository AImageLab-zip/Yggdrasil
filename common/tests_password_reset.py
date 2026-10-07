"""Password recovery (``common/password_reset.py``), lost in 3.0 and restored for #96."""

import re

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from common.password_reset import RESET_EMAILS_PER_WINDOW

IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)


class PasswordResetTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("forgetful", email="forgetful@example.org", password="old-password-123")

    def _request(self, email="forgetful@example.org", **extra):
        return self.client.post(reverse("password_reset"), {"email": email}, **extra)

    def _reset_path(self, message):
        return re.search(r"http://testserver(/password-reset/[^/\s]+/[^/\s]+/)", message.body).group(1)

    def test_login_page_links_to_the_reset_form(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, f'href="{reverse("password_reset")}"')
        self.assertEqual(self.client.get(reverse("password_reset")).status_code, 200)

    def test_request_sends_the_branded_email(self):
        response = self._request()

        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["forgetful@example.org"])
        self.assertIn("forgetful", message.body)
        self.assertIn("expires in 3 days", message.body)
        html, mimetype = message.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn(f'href="http://testserver{self._reset_path(message)}"', html)
        self.assertIn('src="cid:yggdrasil-logo"', html)
        logo = [part for part in message.message().walk() if part.get("Content-ID") == "<yggdrasil-logo>"]
        self.assertEqual(len(logo), 1)

    def test_unknown_address_gets_the_same_answer_and_no_email(self):
        response = self._request("nobody@example.org")

        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(DEMO_GUEST_USERNAME="guest")
    def test_the_demo_guest_cannot_be_reset(self):
        # The worst case the guard exists for: a hand-made guest with an address and a password.
        guest, _ = User.objects.get_or_create(username="guest")
        guest.email = "demo@example.org"
        guest.set_password("shared-guest-pw")
        guest.save()

        self._request("demo@example.org")

        self.assertEqual(len(mail.outbox), 0)

    def test_one_address_cannot_be_flooded(self):
        for _ in range(RESET_EMAILS_PER_WINDOW + 2):
            response = self._request()
            self.assertRedirects(response, reverse("password_reset_done"))

        self.assertEqual(len(mail.outbox), RESET_EMAILS_PER_WINDOW)

    def test_the_link_sets_a_new_password_once(self):
        self._request()
        reset_path = self._reset_path(mail.outbox[0])

        # Django swaps the token for a session marker and redirects to the form.
        form_url = self.client.get(reset_path).url
        response = self.client.post(
            form_url, {"new_password1": "a-new-long-password-9", "new_password2": "a-new-long-password-9"}
        )

        self.assertRedirects(response, reverse("password_reset_complete"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("a-new-long-password-9"))
        self.assertTrue(self.client.login(username="forgetful", password="a-new-long-password-9"))
        self.client.logout()
        self.assertContains(self.client.get(reset_path), "This link no longer works")

    def test_a_forged_link_is_refused(self):
        response = self.client.get(reverse("password_reset_confirm", args=["MQ", "set-password-xyz"]))
        self.assertContains(response, "This link no longer works")

    @override_settings(AXES_ENABLED=True, AXES_FAILURE_LIMIT=3)
    def test_a_reset_lifts_a_login_lockout(self):
        for _ in range(3):
            self.client.post(reverse("login"), {"username": "forgetful", "password": "wrong"}, REMOTE_ADDR="203.0.113.7")
        self._request()
        form_url = self.client.get(self._reset_path(mail.outbox[0])).url
        self.client.post(form_url, {"new_password1": "a-new-long-password-9", "new_password2": "a-new-long-password-9"})

        response = self.client.post(
            reverse("login"),
            {"username": "forgetful", "password": "a-new-long-password-9"},
            REMOTE_ADDR="203.0.113.7",
        )
        self.assertEqual(response.status_code, 302)

    def test_a_phone_can_reset_its_password_too(self):
        # Phones sign in since #102, so they recover their password the same way.
        response = self.client.get(reverse("password_reset"), HTTP_USER_AGENT=IPHONE)
        self.assertTemplateUsed(response, "registration/password_reset_form.html")

        self._request(HTTP_USER_AGENT=IPHONE)
        self.assertEqual(len(mail.outbox), 1)
