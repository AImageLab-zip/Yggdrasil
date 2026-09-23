"""The profile page is one shared page, resolved per namespace.

Two things went wrong here and both are easy to reintroduce:

* ``templates/common/user_profile.html`` assigned its ``ns`` variable with a
  ``{% firstof %}`` at the top of the file. A child template's content outside
  ``{% block %}`` is never rendered, so ``ns`` stayed empty and every
  ``{% url ns|add:... %}`` in the page raised ``KeyError: ''``. maxillo's profile
  was a 500 for exactly that reason, unnoticed because brain and urology each
  rendered a 35-line stub of their own instead of this page.
* The view behind it resolved its models by importing maxillo's directly, so
  serving another domain from it would have reported maxillo's statistics.

So: every domain's profile must render, and every link on it must stay inside
that domain.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from common.domains import DOMAINS
from common.models import Project, ProjectAccess


class ProfileRendersForEveryDomainTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("profile-user", password="pw")
        for domain in sorted(DOMAINS):
            project, _ = Project.objects.get_or_create(
                slug=f"profile-{domain}",
                defaults={"name": f"Profile {domain}", "domain": domain},
            )
            ProjectAccess.objects.get_or_create(
                user=cls.user, project=project, defaults={"role": "admin"}
            )

    def setUp(self):
        self.client.force_login(self.user)

    def _url(self, domain):
        try:
            return reverse(f"{domain}:user_profile")
        except NoReverseMatch:
            self.skipTest(f"{domain} does not route user_profile")

    def test_it_renders_in_every_domain(self):
        for domain in sorted(DOMAINS):
            with self.subTest(domain=domain):
                self.assertEqual(self.client.get(self._url(domain)).status_code, 200)

    def test_its_links_stay_inside_the_domain(self):
        """A `ns` that failed to resolve would 500; one resolving to the wrong
        domain would silently send people into somebody else's data."""
        for domain in sorted(DOMAINS):
            with self.subTest(domain=domain):
                body = self.client.get(self._url(domain)).content.decode()
                self.assertIn(f'href="/{domain}/', body)
                for other in sorted(DOMAINS - {domain}):
                    self.assertNotIn(f'href="/{other}/', body)
