"""Phones sign in like any other device (#102).

From 3.3 to this change a phone was refused sign-in and registration and dropped
into the read-only public demo instead, with a "use a computer" interstitial over
every signed-in page. Annotating from a phone -- an ECG rhythm on a ward round --
needs the opposite, so the policy is gone: the same login form, the same pages, no
gate. These tests pin that a phone is treated exactly as a desktop, and that the
demo is still one tap away for a visitor without an account.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from common.models import Project, ProjectAccess

IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
ANDROID_PHONE = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)
DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: The 3.3 interstitial's root. Nothing may render it any more.
GATE_MARKUP = "ygg-desktop-gate"


class PhoneSignInTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.password = "correct horse battery staple"
        cls.alice = User.objects.create_user(username="alice", password=cls.password)
        cls.guest = User.objects.get(username=settings.DEMO_GUEST_USERNAME)
        project = Project.objects.create(
            name="Maxillo demo", slug="maxillo-demo", domain="maxillo", is_active=True
        )
        ProjectAccess.objects.create(user=cls.guest, project=project, role="viewer")

    def _login(self, ua, next_url=None):
        url = reverse("login") + (f"?next={next_url}" if next_url else "")
        return self.client.post(
            url, {"username": "alice", "password": self.password}, HTTP_USER_AGENT=ua
        )

    # --- sign-in ---
    def test_phone_gets_the_sign_in_form(self):
        r = self.client.get(reverse("login"), HTTP_USER_AGENT=IPHONE)
        self.assertEqual(r.status_code, 200)
        self.assertTemplateUsed(r, "registration/login.html")
        self.assertContains(r, 'name="password"')
        # Not silently signed in as the demo guest any more.
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_phone_signs_in_with_the_right_password(self):
        r = self._login(ANDROID_PHONE)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.alice.pk)

    def test_a_link_opened_on_a_phone_lands_there_after_sign_in(self):
        # The link a colleague sent: @login_required bounces it via /login/?next=.
        target = reverse("cardiology:patient_list")
        r = self.client.get(target, HTTP_USER_AGENT=IPHONE)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith(reverse("login")))

        r = self._login(IPHONE, next_url=target)
        self.assertRedirects(r, target, fetch_redirect_response=False)

    def test_the_sign_in_page_still_offers_the_demo(self):
        r = self.client.get(reverse("login"), HTTP_USER_AGENT=IPHONE)
        self.assertContains(r, reverse("demo:index"))

    def test_phone_and_desktop_get_the_same_sign_in_page(self):
        phone = self.client.get(reverse("login"), HTTP_USER_AGENT=IPHONE)
        desktop = self.client.get(reverse("login"), HTTP_USER_AGENT=DESKTOP)
        self.assertEqual(phone.templates[0].name, desktop.templates[0].name)
        self.assertNotIn("User-Agent", phone.get("Vary", ""))

    # --- registration and recovery ---
    def test_phone_can_open_an_invitation_and_register(self):
        # The invitation email's link: the form renders, code prefilled.
        r = self.client.get(reverse("register") + "?code=phone-invite", HTTP_USER_AGENT=ANDROID_PHONE)
        self.assertTemplateUsed(r, "registration/register.html")
        self.assertContains(r, 'name="password1"')

    def test_phone_can_ask_for_a_password_reset(self):
        r = self.client.get(reverse("password_reset"), HTTP_USER_AGENT=IPHONE)
        self.assertTemplateUsed(r, "registration/password_reset_form.html")

    # --- the landing page ---
    def test_phone_landing_offers_sign_in_and_the_demo(self):
        r = self.client.get(reverse("home"), HTTP_USER_AGENT=IPHONE)
        self.assertContains(r, f'href="{reverse("login")}"')
        self.assertContains(r, f'href="{reverse("register")}"')
        self.assertContains(r, reverse("demo:index"))

    # --- no desktop gate ---
    def test_a_signed_in_user_on_a_phone_gets_the_page_not_a_gate(self):
        self.client.force_login(self.alice)
        for url in (reverse("home"), reverse("cardiology:patient_list")):
            with self.subTest(url=url):
                r = self.client.get(url, HTTP_USER_AGENT=IPHONE, follow=True)
                self.assertEqual(r.status_code, 200)
                self.assertNotContains(r, GATE_MARKUP)

    def test_the_demo_is_still_open_to_phones(self):
        r = self.client.get(reverse("demo:index"), HTTP_USER_AGENT=IPHONE)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.guest.pk)
        r = self.client.get(reverse("home"), HTTP_USER_AGENT=IPHONE)
        self.assertNotContains(r, GATE_MARKUP)
