from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from common.permissions import user_has_project_access, user_is_project_admin


class PermissionHelpersTakeAProjectTests(TestCase):
    """A request or a domain name is no longer an authorization context.

    Both used to resolve to the session's (or the domain's first) project, so an
    admin of project A passed checks on project B's patients. The helpers now
    refuse them outright instead of guessing.
    """

    def test_a_domain_name_is_refused(self):
        from django.contrib.auth.models import User

        user = User.objects.create_user("ctx")
        for helper in (user_is_project_admin, user_has_project_access):
            with self.subTest(helper=helper.__name__):
                with self.assertRaises(TypeError):
                    helper(user, "maxillo")

    def test_a_request_is_refused(self):
        from django.contrib.auth.models import User

        user = User.objects.create_user("ctx")
        request = SimpleNamespace(
            user=user, session={}, resolver_match=SimpleNamespace(namespace="brain")
        )
        with self.assertRaises(TypeError):
            user_is_project_admin(user, request)

    def test_no_project_denies(self):
        from django.contrib.auth.models import User

        user = User.objects.create_user("ctx")
        self.assertFalse(user_is_project_admin(user, None))
        self.assertFalse(user_has_project_access(user, None))


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
        from django.apps import apps

        from common.models import Project
        from laparoscopy.models import Patient as LaparoscopyPatient
        from maxillo.models import Patient as MaxilloPatient
        from urology.models import Patient as UrologyPatient

        # By name: a new import from `common` into a domain app needs an import-linter
        # exception, and those are not added.
        CardiologyPatient = apps.get_model("cardiology", "Patient")

        with mock.patch("common.signals.celery_app.send_task"):
            call_command("seed_dev")
            call_command("seed_dev")

        self.assertEqual(
            set(Project.objects.values_list("slug", flat=True)),
            {"maxillo", "brain", "laparoscopy", "urology", "cardiology"},
        )
        for model in (MaxilloPatient, BrainPatient, LaparoscopyPatient, UrologyPatient, CardiologyPatient):
            self.assertEqual(model.objects.filter(name="Demo Patient").count(), 1)

        from django.contrib.auth.models import User

        admin = User.objects.get(username="admin")
        self.assertTrue(admin.is_superuser)
        self.assertEqual(admin.project_access.count(), 5)


class UrlSmokeTests(TestCase):
    """Zero-fixture checks that the main entry points render at all.

    Catches URLConf, template and import breakage without exercising any
    domain logic.
    """

    def test_landing_renders_for_anonymous_user(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_app_indexes_redirect_anonymous_user_to_login(self):
        for path in ("/maxillo/", "/brain/", "/laparoscopy/", "/urology/", "/cardiology/"):
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
        self.assertContains(response, "Confocale")
