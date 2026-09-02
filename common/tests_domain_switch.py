"""Crossing from one domain to another must land in an accessible project.

The session's ``current_project_id`` is domain-scoped. When it was left
pointing at the previous domain's project, ``ActiveProfileMiddleware`` resolved
the *alphabetically* first project of the new domain -- typically one the user
has no ``ProjectAccess`` for -- and bounced them back to the landing page, so a
user with access in every domain could only ever enter the one they happened to
select first.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from common.models import Project, ProjectAccess


class DomainSwitchTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="marco", password="pw")
        self.maxillo, _ = Project.objects.get_or_create(
            slug="maxillo", defaults={"name": "Maxillo", "domain": "maxillo"}
        )
        # Sorts before "Projects Showcase": the project the old fallback picked.
        self.brain_other = Project.objects.create(name="Alpha Study", slug="brain-alpha", domain="brain")
        self.brain = Project.objects.create(name="Projects Showcase", slug="brain-showcase", domain="brain")
        for project in (self.maxillo, self.brain):
            ProjectAccess.objects.create(user=self.user, project=project, role="viewer")
        self.client.force_login(self.user)

    def test_entering_brain_after_maxillo_uses_the_accessible_project(self):
        self.client.get("/maxillo/")
        self.assertEqual(self.client.session["current_project_id"], self.maxillo.id)

        response = self.client.get("/brain/", follow=True)

        self.assertEqual(self.client.session["current_project_id"], self.brain.id)
        self.assertNotEqual(response.request["PATH_INFO"], "/")
        self.assertEqual(response.status_code, 200)

    def test_a_domain_without_access_still_refuses(self):
        ProjectAccess.objects.filter(user=self.user, project=self.brain).delete()

        response = self.client.get("/brain/", follow=True)

        self.assertEqual(response.request["PATH_INFO"], "/")
