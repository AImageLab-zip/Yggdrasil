"""Phones get the public demo and nothing else (``common/mobile.py``).

The policy has two halves and both are pinned here: the server refuses sign-in and
registration from a phone -- and lets it straight into the demo instead, at whatever
link it opened -- and the desktop gate in ``base.html`` stays only on a real
signed-in user's pages, so the demo guest and anonymous visitors get a phone layout.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse

from common.mobile import LOGIN_MOBILE_TEMPLATE, is_mobile_request
from common.models import Project, ProjectAccess

IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
ANDROID_PHONE = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)
ANDROID_TABLET = (
    "Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# iPadOS 13+ asks for the desktop site by default and says it is a Mac.
IPAD_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"
)
# Older iPads still carry the "Mobile" token.
IPAD_LEGACY = (
    "Mozilla/5.0 (iPad; CPU OS 12_2 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/12.1 Mobile/15E148 Safari/604.1"
)
DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

GATE_MARKUP = 'class="ygg-desktop-gate"'


class IsMobileRequestTests(TestCase):
    def setUp(self):
        self.rf = RequestFactory()

    def _req(self, **meta):
        return self.rf.get("/", **meta)

    def test_phones_are_mobile(self):
        for ua in (IPHONE, ANDROID_PHONE):
            with self.subTest(ua=ua):
                self.assertTrue(is_mobile_request(self._req(HTTP_USER_AGENT=ua)))

    def test_tablets_and_desktops_are_not(self):
        for ua in (ANDROID_TABLET, IPAD_DESKTOP_UA, IPAD_LEGACY, DESKTOP, ""):
            with self.subTest(ua=ua):
                self.assertFalse(is_mobile_request(self._req(HTTP_USER_AGENT=ua)))

    def test_client_hint_wins_over_the_user_agent(self):
        self.assertTrue(is_mobile_request(
            self._req(HTTP_USER_AGENT=DESKTOP, HTTP_SEC_CH_UA_MOBILE="?1")
        ))
        self.assertFalse(is_mobile_request(
            self._req(HTTP_USER_AGENT=ANDROID_PHONE, HTTP_SEC_CH_UA_MOBILE="?0")
        ))

    def test_no_request_is_not_mobile(self):
        self.assertFalse(is_mobile_request(None))


class MobileAuthPolicyTests(TestCase):
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

    def _login(self, ua):
        return self.client.post(
            reverse("login"),
            {"username": "alice", "password": self.password},
            HTTP_USER_AGENT=ua,
        )

    # --- sign-in ---
    def test_phone_opening_login_goes_straight_into_the_demo(self):
        r = self.client.get(reverse("login"), HTTP_USER_AGENT=IPHONE)
        self.assertRedirects(r, reverse("home"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.guest.pk)

    def test_phone_opening_any_link_lands_on_that_link_in_the_demo(self):
        # The link a visitor was given: @login_required bounces it via /login/?next=.
        target = reverse("maxillo:patient_list")
        r = self.client.get(target, HTTP_USER_AGENT=IPHONE)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith(reverse("login")))

        r = self.client.get(r["Location"], HTTP_USER_AGENT=IPHONE)
        self.assertRedirects(r, target, fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.guest.pk)

    def test_phone_next_to_another_site_goes_home_instead(self):
        r = self.client.get(
            reverse("login") + "?next=https://evil.example/", HTTP_USER_AGENT=IPHONE
        )
        self.assertRedirects(r, reverse("home"), fetch_redirect_response=False)

    def test_desktop_is_never_auto_entered(self):
        r = self.client.get(
            reverse("login") + "?next=" + reverse("maxillo:patient_list"),
            HTTP_USER_AGENT=DESKTOP,
        )
        self.assertEqual(r.status_code, 200)
        self.assertTemplateUsed(r, "registration/login.html")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_signed_in_real_user_on_a_phone_is_not_swapped_for_the_guest(self):
        self.client.force_login(self.alice)
        r = self.client.get(reverse("login"), HTTP_USER_AGENT=IPHONE)
        self.assertTemplateUsed(r, LOGIN_MOBILE_TEMPLATE)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.alice.pk)

    def test_phone_cannot_sign_in_even_with_the_right_password(self):
        r = self._login(IPHONE)
        self.assertEqual(r.status_code, 403)
        self.assertTemplateUsed(r, LOGIN_MOBILE_TEMPLATE)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_desktop_and_tablet_sign_in_unchanged(self):
        for ua in (DESKTOP, IPAD_DESKTOP_UA, ANDROID_TABLET):
            with self.subTest(ua=ua):
                self.client.logout()
                r = self.client.get(reverse("login"), HTTP_USER_AGENT=ua)
                self.assertTemplateUsed(r, "registration/login.html")
                r = self._login(ua)
                self.assertEqual(r.status_code, 302)
                self.assertEqual(int(self.client.session["_auth_user_id"]), self.alice.pk)

    def test_login_responses_vary_on_the_device(self):
        for ua in (IPHONE, DESKTOP):
            with self.subTest(ua=ua):
                r = self.client.get(reverse("login"), HTTP_USER_AGENT=ua)
                self.assertIn("User-Agent", r["Vary"])
                self.assertIn("Sec-CH-UA-Mobile", r["Vary"])
                self.assertEqual(r["Accept-CH"], "Sec-CH-UA-Mobile")

    def test_phone_without_published_demo_is_told_so(self):
        ProjectAccess.objects.filter(user=self.guest).delete()
        r = self.client.get(reverse("login"), HTTP_USER_AGENT=IPHONE)
        self.assertTemplateUsed(r, LOGIN_MOBILE_TEMPLATE)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertNotContains(r, 'name="password"')
        self.assertContains(r, "not available right now")
        self.assertNotContains(r, reverse("demo:index"))

    # --- registration ---
    def test_phone_cannot_register(self):
        r = self.client.get(reverse("register"), HTTP_USER_AGENT=ANDROID_PHONE)
        self.assertTemplateUsed(r, LOGIN_MOBILE_TEMPLATE)
        r = self.client.post(
            reverse("register"), {"username": "mallory"}, HTTP_USER_AGENT=ANDROID_PHONE
        )
        self.assertEqual(r.status_code, 403)
        self.assertFalse(get_user_model().objects.filter(username="mallory").exists())

    def test_desktop_register_page_unchanged(self):
        r = self.client.get(reverse("register"), HTTP_USER_AGENT=DESKTOP)
        self.assertTemplateUsed(r, "registration/register.html")

    # --- the demo itself ---
    def test_phone_can_enter_the_demo(self):
        r = self.client.get(reverse("demo:index"), HTTP_USER_AGENT=IPHONE)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.guest.pk)

    def test_phone_landing_offers_the_demo_and_no_sign_in(self):
        r = self.client.get(reverse("home"), HTTP_USER_AGENT=IPHONE)
        self.assertContains(r, reverse("demo:index"))
        self.assertNotContains(r, f'href="{reverse("login")}"')
        self.assertNotContains(r, f'href="{reverse("register")}"')

    def test_desktop_landing_still_offers_sign_in(self):
        r = self.client.get(reverse("home"), HTTP_USER_AGENT=DESKTOP)
        self.assertContains(r, f'href="{reverse("login")}"')

    # --- the desktop gate ---
    def test_gate_is_absent_for_anonymous_visitors(self):
        r = self.client.get(reverse("home"), HTTP_USER_AGENT=IPHONE)
        self.assertNotContains(r, GATE_MARKUP)

    def test_gate_is_absent_for_the_demo_guest(self):
        self.client.force_login(self.guest)
        r = self.client.get(reverse("home"), HTTP_USER_AGENT=IPHONE)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, GATE_MARKUP)

    def test_gate_stays_for_a_real_signed_in_user(self):
        self.client.force_login(self.alice)
        r = self.client.get(reverse("home"), HTTP_USER_AGENT=IPHONE)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, GATE_MARKUP)
