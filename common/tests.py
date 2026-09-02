from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from common.permissions import _namespace


def _fake_request(namespace):
    return SimpleNamespace(resolver_match=SimpleNamespace(namespace=namespace))


class NamespaceResolutionTests(TestCase):
    def test_string_namespaces(self):
        self.assertEqual(_namespace("maxillo"), "maxillo")
        self.assertEqual(_namespace("brain"), "brain")
        self.assertEqual(_namespace("laparoscopy"), "laparoscopy")
        self.assertEqual(_namespace("unknown"), "maxillo")

    def test_request_namespace_resolves_to_its_own_domain(self):
        self.assertEqual(_namespace(_fake_request("brain")), "brain")
        self.assertEqual(_namespace(_fake_request("laparoscopy")), "laparoscopy")
        self.assertEqual(_namespace(_fake_request("maxillo")), "maxillo")

    def test_request_without_namespace_falls_back_to_maxillo(self):
        self.assertEqual(_namespace(_fake_request("")), "maxillo")
        self.assertEqual(_namespace(SimpleNamespace(resolver_match=None)), "maxillo")


class AppVersionTests(TestCase):
    def test_app_version_is_semver(self):
        self.assertRegex(settings.APP_VERSION, r"^\d+\.\d+\.\d+")

    def test_app_version_matches_version_file(self):
        version_file = settings.BASE_DIR / "VERSION"
        self.assertEqual(settings.APP_VERSION, version_file.read_text().strip())

    def test_footer_renders_version(self):
        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"v{settings.APP_VERSION}")


class SeedDevCommandTests(TestCase):
    @override_settings(DEBUG=False)
    def test_refuses_to_run_without_debug(self):
        with self.assertRaises(CommandError):
            call_command("seed_dev")

    @override_settings(DEBUG=True)
    def test_seed_is_idempotent(self):
        from brain.models import Patient as BrainPatient
        from common.models import Project
        from laparoscopy.models import Patient as LaparoscopyPatient
        from maxillo.models import Patient as MaxilloPatient

        with mock.patch("common.signals.celery_app.send_task"):
            call_command("seed_dev")
            call_command("seed_dev")

        self.assertEqual(
            set(Project.objects.values_list("slug", flat=True)),
            {"maxillo", "brain", "laparoscopy"},
        )
        for model in (MaxilloPatient, BrainPatient, LaparoscopyPatient):
            self.assertEqual(model.objects.filter(name="Demo Patient").count(), 1)

        from django.contrib.auth.models import User

        admin = User.objects.get(username="admin")
        self.assertTrue(admin.is_superuser)
        self.assertEqual(admin.project_access.count(), 3)


class UrlSmokeTests(TestCase):
    """Zero-fixture checks that the main entry points render at all.

    Catches URLConf, template and import breakage without exercising any
    domain logic.
    """

    def test_landing_renders_for_anonymous_user(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_app_indexes_redirect_anonymous_user_to_login(self):
        for path in ("/maxillo/", "/brain/", "/laparoscopy/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith("/login/"))

    def test_landing_renders_for_staff_user(self):
        from django.contrib.auth.models import User

        User.objects.create_superuser("smoke-admin", password="pw")
        self.client.login(username="smoke-admin", password="pw")
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
