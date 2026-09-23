"""``folder_stats`` is an access-controlled read, in every domain.

It reports how many patients a folder holds. maxillo has always required project
admin (``maxillo/views/folders_tags.py``); brain's fork shipped with no check at
all, and urology inherited that by copying brain.

The boundary that matters is *cross-project*, not anonymous: ``ProjectSessionMiddleware``
already redirects someone with no project in the domain, so the exposure was to a
user who legitimately holds one project and asks about another's folder. That is
the same class of bug the 2.0.0 changelog records as "a member of one project
could read another project's files".

Parametrised over the domains that route the view, so a fifth domain copying an
existing one cannot quietly reintroduce the gap.
"""

from django.apps import apps
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from common.models import Project, ProjectAccess

DOMAINS_WITH_FOLDER_STATS = ("maxillo", "brain", "urology")


class FolderStatsRequiresProjectAdminTests(TestCase):
    """One project's admin must not be able to read another project's folder."""

    def _project(self, domain, suffix, user, role="admin"):
        project, _ = Project.objects.get_or_create(
            slug=f"stats-{domain}-{suffix}",
            defaults={"name": f"Stats {domain} {suffix}", "domain": domain},
        )
        ProjectAccess.objects.get_or_create(
            user=user, project=project, defaults={"role": role}
        )
        return project

    def _url_for_other_projects_folder(self, domain):
        """A folder in project A; the caller will be an admin of project B."""
        owner = User.objects.create_user(f"owner-{domain}", password="pw")
        target = self._project(domain, "a", owner)
        folder = apps.get_model(domain, "Folder").objects.create(
            name="Stats", project=target, created_by=owner
        )
        try:
            return reverse(f"{domain}:folder_stats", args=[folder.id]), folder
        except NoReverseMatch:
            self.skipTest(f"{domain} does not route folder_stats")

    def test_an_admin_of_a_different_project_is_refused(self):
        for domain in DOMAINS_WITH_FOLDER_STATS:
            with self.subTest(domain=domain):
                url, _folder = self._url_for_other_projects_folder(domain)
                intruder = User.objects.create_user(f"intruder-{domain}", password="pw")
                # Real access, to a different project in the same domain, so the
                # session middleware lets the request through to the view.
                self._project(domain, "b", intruder)
                self.client.force_login(intruder)
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_the_owning_projects_admin_gets_the_count(self):
        for domain in DOMAINS_WITH_FOLDER_STATS:
            with self.subTest(domain=domain):
                url, folder = self._url_for_other_projects_folder(domain)
                admin = User.objects.get(username=f"owner-{domain}")
                self.client.force_login(admin)
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["stats"]["patient_count"], 0)

    def test_an_anonymous_visitor_never_reaches_it(self):
        for domain in DOMAINS_WITH_FOLDER_STATS:
            with self.subTest(domain=domain):
                url, _folder = self._url_for_other_projects_folder(domain)
                self.client.logout()
                self.assertIn(self.client.get(url).status_code, (302, 403))
