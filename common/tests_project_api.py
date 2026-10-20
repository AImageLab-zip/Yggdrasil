"""/api/projects/: look a project up, list them, create one."""

import json

from django.contrib.auth.models import User
from django.test import TestCase

from common.models import AnnotationMethod, Modality, Project, ProjectAccess


class ProjectApiTestBase(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser("root", password="pw")
        self.member = User.objects.create_user("member", password="pw")
        self.outsider = User.objects.create_user("outsider", password="pw")
        self.project = Project.objects.create(name="Seen", slug="seen", domain="brain")
        self.hidden = Project.objects.create(name="Hidden", slug="hidden", domain="brain")
        ProjectAccess.objects.create(user=self.member, project=self.project, role="viewer")
        self.brain_mod = Modality.objects.create(name="Test T1", slug="test-t1", domain="brain")
        self.any_mod = Modality.objects.create(name="Test Any", slug="test-any", domain="")
        self.maxillo_mod = Modality.objects.create(name="Test CBCT", slug="test-cbct", domain="maxillo")
        self.brain_method = AnnotationMethod.objects.create(name="Test Seg", slug="test-seg", domain="brain")

    def create(self, user, **body):
        self.client.force_login(user)
        return self.client.post("/api/projects/", json.dumps(body), content_type="application/json")


class ProjectLookupTests(ProjectApiTestBase):
    def test_a_member_sees_their_project(self):
        self.client.force_login(self.member)
        response = self.client.get("/api/projects/seen/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["domain"], "brain")
        self.assertEqual(response.json()["role"], "viewer")

    def test_unreadable_and_missing_look_the_same(self):
        self.client.force_login(self.member)
        hidden = self.client.get("/api/projects/hidden/")
        missing = self.client.get("/api/projects/no-such-project/")
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(hidden.json(), missing.json())

    def test_anonymous_gets_401_not_a_redirect(self):
        self.assertEqual(self.client.get("/api/projects/seen/").status_code, 401)
        self.assertEqual(self.client.get("/api/projects/").status_code, 401)

    def test_the_list_holds_only_readable_projects(self):
        self.client.force_login(self.member)
        slugs = [p["slug"] for p in self.client.get("/api/projects/").json()["projects"]]
        self.assertEqual(slugs, ["seen"])

    def test_the_list_filters_by_domain_and_name(self):
        self.client.force_login(self.root)
        response = self.client.get("/api/projects/", {"domain": "brain", "name": "Hidden"})
        self.assertEqual([p["slug"] for p in response.json()["projects"]], ["hidden"])


class ProjectCreateTests(ProjectApiTestBase):
    def test_a_superuser_creates_a_project(self):
        response = self.create(
            self.root, name="New Study", domain="brain",
            modalities=["test-t1", "test-any"], annotation_methods=["test-seg"],
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["slug"], "new-study")
        self.assertEqual(body["modalities"], ["test-any", "test-t1"])
        self.assertEqual(response["Location"], "/api/projects/new-study/")
        project = Project.objects.get(slug="new-study")
        self.assertEqual(project.created_by, self.root)
        self.assertEqual(ProjectAccess.objects.get(project=project, user=self.root).role, "admin")

    def test_only_superusers_may(self):
        staff = User.objects.create_user("staff", password="pw", is_staff=True)
        for user in (self.member, staff):
            with self.subTest(user=user.username):
                self.assertEqual(self.create(user, name="Nope", domain="brain").status_code, 403)
        self.assertFalse(Project.objects.filter(name="Nope").exists())

    def test_a_duplicate_is_a_409_naming_the_existing_slug(self):
        response = self.create(self.root, name="Seen", domain="brain")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["slug"], "seen")
        # Same name in another domain is a different project...
        self.assertEqual(self.create(self.root, name="Seen", domain="urology", slug="seen-uro").status_code, 201)
        # ...but slugs are global.
        self.assertEqual(self.create(self.root, name="Other", domain="urology", slug="seen").status_code, 409)

    def test_invalid_bodies_are_400(self):
        cases = {
            "no name": {"domain": "brain"},
            "bad domain": {"name": "X", "domain": "dentistry"},
            "cross-domain modality": {"name": "X", "domain": "brain", "modalities": ["test-cbct"]},
            "unknown method": {"name": "X", "domain": "brain", "annotation_methods": ["nope"]},
            "modalities not a list": {"name": "X", "domain": "brain", "modalities": "test-t1"},
            "reserved slug": {"name": "Projects", "domain": "brain"},
            "bad slug": {"name": "X", "domain": "brain", "slug": "Not A Slug"},
            "unsluggable name": {"name": "!!!", "domain": "brain"},
            "long name": {"name": "x" * 51, "domain": "brain"},
        }
        before = Project.objects.count()
        for label, body in cases.items():
            with self.subTest(label):
                self.assertEqual(self.create(self.root, **body).status_code, 400)
        self.assertEqual(Project.objects.count(), before)

    def test_a_non_object_body_is_400(self):
        self.client.force_login(self.root)
        for raw in ("[]", "not json"):
            response = self.client.post("/api/projects/", raw, content_type="application/json")
            self.assertEqual(response.status_code, 400)
