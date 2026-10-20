"""Personal API tokens: who they authenticate, where, and what a bad one costs."""

from datetime import timedelta
from io import StringIO

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from common import api_tokens
from common.api_tokens import issue_token
from common.models import ApiToken, Project, ProjectAccess


class ApiTokenTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("scripter", password="pw")
        self.project = Project.objects.create(name="Tokens", slug="tokens", domain="maxillo")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="annotator")
        self.token, self.key = issue_token(self.user, "test")
        # CSRF is enforced: the default test client turns it off, which would
        # hide exactly the behaviour under test.
        self.client = Client(enforce_csrf_checks=True)

    def bearer(self, key=None):
        return {"HTTP_AUTHORIZATION": f"Bearer {key or self.key}"}


class TokenStorageTests(ApiTokenTestBase):
    def test_only_the_hash_is_stored(self):
        self.assertTrue(self.key.startswith(ApiToken.KEY_PREFIX))
        stored = ApiToken.objects.get(pk=self.token.pk)
        self.assertNotIn(self.key, (stored.key_hash, stored.key_start))
        self.assertEqual(stored.key_hash, ApiToken.hash_key(self.key))
        self.assertTrue(self.key.startswith(stored.key_start))

    def test_the_demo_guest_cannot_hold_one(self):
        guest, _ = User.objects.get_or_create(username=settings.DEMO_GUEST_USERNAME)
        with self.assertRaises(ValueError):
            issue_token(guest, "nope")

    def test_the_command_prints_a_working_token_once(self):
        out = StringIO()
        call_command("create_api_token", "scripter", "--name", "cli", "--days", "3", stdout=out, stderr=StringIO())
        key = out.getvalue().strip()
        token = ApiToken.objects.get(key_hash=ApiToken.hash_key(key))
        self.assertEqual(token.user, self.user)
        self.assertIsNotNone(token.expires_at)
        with self.assertRaises(CommandError):
            call_command("create_api_token", "nobody", "--name", "x", stdout=StringIO(), stderr=StringIO())


class TokenAuthenticationTests(ApiTokenTestBase):
    def test_a_token_authenticates_an_api_request(self):
        response = self.client.get("/api/projects/tokens/", **self.bearer())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "annotator")

    def test_no_session_is_created(self):
        self.client.get("/api/projects/tokens/", **self.bearer())
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_without_a_token_the_api_is_anonymous(self):
        self.assertEqual(self.client.get("/api/projects/tokens/").status_code, 401)

    def test_a_bad_token_is_refused_even_with_a_session(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/projects/tokens/", **self.bearer("ygg_wrong"))
        self.assertEqual(response.status_code, 401)

    def test_revoked_expired_and_inactive_are_refused(self):
        cases = [
            {"revoked_at": timezone.now()},
            {"expires_at": timezone.now() - timedelta(seconds=1)},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                ApiToken.objects.filter(pk=self.token.pk).update(**{"revoked_at": None, "expires_at": None, **fields})
                self.assertEqual(self.client.get("/api/projects/tokens/", **self.bearer()).status_code, 401)
        ApiToken.objects.filter(pk=self.token.pk).update(revoked_at=None, expires_at=None)
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.client.get("/api/projects/tokens/", **self.bearer()).status_code, 401)

    def test_html_pages_ignore_tokens(self):
        # Outside /api/ a token is not a login: the page still redirects to it.
        response = self.client.get("/maxillo/", **self.bearer())
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_the_runner_api_is_untouched(self):
        # A personal token is not a runner token, and the middleware must not
        # answer for the runner API either way: its own check decides.
        with override_settings(RUNNER_API_TOKENS={"runner-secret"}):
            response = self.client.post("/api/runner/jobs/1/claim/", **self.bearer())
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("API token", response.json().get("error", ""))

    def test_last_used_is_recorded(self):
        self.client.get("/api/projects/tokens/", **self.bearer())
        self.token.refresh_from_db()
        self.assertIsNotNone(self.token.last_used_at)


class TokenCsrfTests(ApiTokenTestBase):
    def setUp(self):
        super().setUp()
        self.user.is_superuser = self.user.is_staff = True
        self.user.save()

    def _create(self, **extra):
        return self.client.post(
            "/api/projects/", '{"name": "Csrf", "domain": "brain"}',
            content_type="application/json", **extra,
        )

    def test_a_token_request_needs_no_csrf_token(self):
        self.assertEqual(self._create(**self.bearer()).status_code, 201)

    def test_a_session_request_still_needs_one(self):
        self.client.force_login(self.user)
        self.assertEqual(self._create().status_code, 403)
        self.assertFalse(Project.objects.filter(name="Csrf").exists())


class TokenThrottleTests(ApiTokenTestBase):
    def test_repeated_bad_tokens_lock_the_address_out(self):
        for _ in range(api_tokens.FAILURE_LIMIT):
            self.client.get("/api/projects/tokens/", REMOTE_ADDR="203.0.113.5", **self.bearer("ygg_guess"))
        # Even the right token is refused from that address now...
        response = self.client.get("/api/projects/tokens/", REMOTE_ADDR="203.0.113.5", **self.bearer())
        self.assertEqual(response.status_code, 429)
        # ...but not from another one.
        response = self.client.get("/api/projects/tokens/", REMOTE_ADDR="198.51.100.2", **self.bearer())
        self.assertEqual(response.status_code, 200)

    def test_good_tokens_do_not_count(self):
        for _ in range(api_tokens.FAILURE_LIMIT + 1):
            response = self.client.get("/api/projects/tokens/", REMOTE_ADDR="203.0.113.6", **self.bearer())
        self.assertEqual(response.status_code, 200)


class TokenAdminTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser("root", password="pw")
        self.staff = User.objects.create_user("staffer", password="pw", is_staff=True)
        self.target = User.objects.create_user("target")

    def test_superuser_sees_the_key_once(self):
        self.client.force_login(self.superuser)
        response = self.client.post(
            "/admin/common/apitoken/add/",
            {"user": self.target.pk, "name": "from admin", "expires_at_0": "", "expires_at_1": ""},
            follow=True,
        )
        token = ApiToken.objects.get(name="from admin")
        self.assertEqual(token.user, self.target)
        self.assertEqual(token.created_by, self.superuser)
        shown = [str(m) for m in response.context["messages"]]
        self.assertTrue(any(token.key_start in m for m in shown), shown)

    def test_the_add_page_documents_the_api(self):
        self.client.force_login(self.superuser)
        response = self.client.get("/admin/common/apitoken/add/")
        self.assertContains(response, "Authorization: Bearer")
        self.assertContains(response, "/api/projects/")

    def test_staff_cannot_reach_tokens(self):
        # Even with every model permission: issuing a token for someone else is
        # becoming them.
        from django.contrib.auth.models import Permission

        self.staff.user_permissions.set(Permission.objects.filter(codename__endswith="apitoken"))
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/admin/common/apitoken/add/").status_code, 403)
        self.assertEqual(self.client.get("/admin/common/apitoken/").status_code, 403)
