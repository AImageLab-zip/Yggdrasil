"""The public changelog page, and the branded error templates.

The page is reached by clicking the version in the footer, which renders on
``/login/``, the landing page and the anonymous demo -- so the thing most worth
pinning is that it stays reachable *without* a session, and stays reachable
while the site is locked down. Both are properties a later decorator or a
tightened middleware would silently take away.
"""

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from common.models import SiteMaintenance


class ChangelogPageTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse("changelog_page")

    def test_an_anonymous_visitor_gets_the_page(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_it_lists_every_released_version(self):
        body = self.client.get(self.url).content.decode()
        for version in ("3.0.0", "2.0.0", "1.9.0"):
            self.assertIn(version, body)

    def test_an_empty_unreleased_section_is_hidden(self):
        """At release time the ``Unreleased`` placeholder carries no
        information, so the page opens on the latest numbered release."""
        body = self.client.get(self.url).content.decode()
        self.assertNotIn("Unreleased", body)
        self.assertIn("Version 3.0.0", body)

    def test_parse_skips_an_empty_unreleased_section(self):
        """Guard against the placeholder recurring mid-cycle: an empty
        ``Unreleased`` never renders, a non-empty one still does."""
        import tempfile
        from pathlib import Path

        from common.views import _parse_changelog

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "CHANGELOG.md"
            path.write_text(
                "## [Unreleased]\n\nNothing yet.\n\n## [3.0.0] - 2026-09-02\n",
                encoding="utf-8",
            )
            self.assertEqual(
                [r["version"] for r in _parse_changelog(path)], ["3.0.0"]
            )
            path.write_text(
                "## [Unreleased]\n\n### Added\n\n- Something new.\n\n"
                "## [3.0.0] - 2026-09-02\n",
                encoding="utf-8",
            )
            self.assertEqual(
                [r["version"] for r in _parse_changelog(path)],
                ["Unreleased", "3.0.0"],
            )

    def test_anonymous_chrome_is_slim_but_public(self):
        """No login gate, no deprecated top nav: anonymous visitors get the
        logo-only rail and the footer version link."""
        body = self.client.get(self.url).content.decode()
        self.assertIn("ygg-rail", body)
        self.assertNotIn("ygg-nav", body)

    def test_authenticated_visitor_gets_the_rail_shell(self):
        """Same shell as the control panel: rail + breadcrumb topbar."""
        User.objects.create_superuser("changelog-admin", password="pw")
        self.client.login(username="changelog-admin", password="pw")
        body = self.client.get(self.url).content.decode()
        self.assertIn("ygg-rail", body)
        self.assertIn("ygg-topbar", body)
        self.assertIn("What's new", body)
        self.assertNotIn("ygg-nav", body)

    def test_it_credits_the_original_authors(self):
        """Phase 6.1 dropped the author credit from the footer; this page is
        where it came back. A rewrite that loses the names is a regression."""
        body = self.client.get(self.url).content.decode()
        self.assertIn("Luca Lumetti", body)
        self.assertIn("Lorenzo Borghi", body)

    def test_it_survives_lockdown(self):
        """``/changelog/`` is in ``SiteMaintenanceMiddleware.PUBLIC_PATHS``.

        Without that entry the page redirects to ``/maintenance/``, which itself
        shows a version link pointing back here -- a loop the visitor cannot
        leave.
        """
        maintenance, _ = SiteMaintenance.objects.get_or_create(pk=1)
        maintenance.access_mode = SiteMaintenance.MODE_LOCKDOWN
        maintenance.save()
        self.addCleanup(
            lambda: SiteMaintenance.objects.filter(pk=1).update(
                access_mode=SiteMaintenance.MODE_NORMAL
            )
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_the_footer_version_links_here(self):
        body = self.client.get("/login/").content.decode()
        self.assertIn(f'href="{self.url}"', body)

    def test_a_broken_changelog_does_not_break_the_page(self):
        """``_load_changelog`` swallows its own failure by design: an unreadable
        CHANGELOG.md must not take the site down at import."""
        from common import views

        with self.settings(BASE_DIR="/nonexistent"):
            self.assertEqual(views._load_changelog(), [])


@override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
class ErrorTemplateTests(TestCase):
    def test_a_missing_page_renders_the_branded_404(self):
        response = self.client.get("/no-such-page-exists/")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Yggdrasil", status_code=404)

    def test_the_500_template_needs_no_context(self):
        """``500.html`` renders with no context processors, so a reference to
        ``app_version`` or ``request`` would raise *while already handling an
        error*. Rendering it bare is the only way to catch that."""
        from django.template.loader import get_template

        get_template("500.html").render({})
