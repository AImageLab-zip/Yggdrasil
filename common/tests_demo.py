"""Guest-demo isolation tests.

These lock the security invariant: the shared read-only demo guest reads exactly
the projects it holds a role on -- and nothing else -- can never write, and the
/demo/ entry point logs it into the real portal at the domain chooser.

Guest scoping used to run off a ``Folder.is_demo`` flag that bypassed
``ProjectAccess`` entirely. It is now ordinary project access, so these tests
assert the boundary through the same code path every other user takes.
"""

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from common.demo import demo_is_published, is_demo_guest, landing_demo_url
from common.models import FileRegistry, Project, ProjectAccess
from common.permissions import (
    filter_folders_for_user,
    filter_patients_for_user,
    user_can_read_folder,
    user_can_read_patient,
    user_can_write_annotations,
)


class DemoIsolationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Folder = apps.get_model("maxillo", "Folder")
        Patient = apps.get_model("maxillo", "Patient")
        cls.Folder = Folder
        cls.Patient = Patient
        # One published project and one the guest was never granted: the pair is
        # the whole boundary now that folders carry no demo flag.
        cls.project = Project.objects.create(
            name="Maxillo demo", slug="maxillo-demo", domain="maxillo", is_active=True
        )
        cls.other_project = Project.objects.create(
            name="Maxillo private", slug="maxillo-private", domain="maxillo",
            is_active=True,
        )
        cls.demo_folder = Folder.objects.create(name="Demo", project=cls.project)
        cls.priv_folder = Folder.objects.create(
            name="Private", project=cls.other_project
        )
        cls.demo_patient = Patient.objects.create(
            name="Demo Pt", folder=cls.demo_folder, project=cls.project,
            visibility="public",
        )
        cls.priv_patient = Patient.objects.create(
            name="Private Pt", folder=cls.priv_folder, project=cls.other_project,
            visibility="private",
        )
        cls.demo_file = FileRegistry.objects.create(
            patient=cls.demo_patient, file_path="raw/demo/x.png",
            file_type="rgb_image", domain="maxillo", file_size=1, file_hash="d",
        )
        cls.priv_file = FileRegistry.objects.create(
            patient=cls.priv_patient, file_path="raw/priv/y.png",
            file_type="rgb_image", domain="maxillo", file_size=1, file_hash="p",
        )

        User = get_user_model()
        cls.guest = User.objects.get(username=settings.DEMO_GUEST_USERNAME)
        # Publishing a project to the demo *is* granting the guest `viewer`.
        cls.grant = ProjectAccess.objects.create(
            user=cls.guest, project=cls.project, role="viewer"
        )
        cls.normal = User.objects.create_user(username="alice", password="x")

    # --- publication gate ---
    def test_demo_is_published_follows_the_grant(self):
        self.assertTrue(demo_is_published())
        self.assertEqual(landing_demo_url(), reverse("demo:index"))
        self.grant.delete()
        self.assertFalse(demo_is_published())
        self.assertIsNone(landing_demo_url())

    def test_invalid_role_does_not_publish(self):
        """A role outside READ_ROLES grants nothing -- the 0036 seed rows."""
        self.grant.delete()
        ProjectAccess.objects.create(
            user=self.guest, project=self.project, role="standard"
        )
        self.assertFalse(demo_is_published())

    # --- guest identity ---
    def test_is_demo_guest_helper(self):
        self.assertTrue(is_demo_guest(self.guest))
        self.assertFalse(is_demo_guest(self.normal))

    # --- permission scoping (the real security boundary) ---
    def test_guest_reads_only_granted_project(self):
        # No context argument: the folder's own project is the one that decides.
        self.assertTrue(user_can_read_folder(self.guest, self.demo_folder))
        self.assertFalse(user_can_read_folder(self.guest, self.priv_folder))

    def test_guest_patient_read_scoped_to_granted_project(self):
        # The production path for a patient, which passes `patient.project`.
        self.assertTrue(user_can_read_patient(self.guest, self.demo_patient))
        self.assertFalse(user_can_read_patient(self.guest, self.priv_patient))

    def test_guest_patient_list_scoped_to_granted_project(self):
        pks = set(
            filter_patients_for_user(
                self.guest, self.Patient.objects.all(), "maxillo"
            ).values_list("pk", flat=True)
        )
        self.assertEqual(pks, {self.demo_patient.pk})

    def test_guest_folder_list_scoped_to_granted_project(self):
        ids = set(
            filter_folders_for_user(
                self.guest, self.Folder.objects.all(), "maxillo"
            ).values_list("id", flat=True)
        )
        self.assertEqual(ids, {self.demo_folder.id})

    def test_folder_project_beats_the_session_context(self):
        """A folder is judged by its own project, not the one you are viewing.

        Callers pass ``request`` or a domain slug, which resolves to the
        session's *current* project. While that won, access to one project
        authorized reading a folder in another.
        """
        self.assertFalse(
            user_can_read_folder(self.guest, self.priv_folder, "maxillo")
        )
        self.assertFalse(
            user_can_write_annotations(self.normal, self.priv_folder, "maxillo")
        )

    def test_guest_cannot_write(self):
        self.assertFalse(
            user_can_write_annotations(self.guest, self.demo_folder, "maxillo")
        )

    def test_guest_cannot_write_even_if_granted_a_writing_role(self):
        """Defence in depth: a mis-granted role must not make the guest writable."""
        self.grant.role = "annotator"
        self.grant.save(update_fields=["role"])
        self.assertFalse(
            user_can_write_annotations(self.guest, self.demo_folder, "maxillo")
        )

    # --- HTTP layer ---
    def test_demo_index_logs_in_and_lands_on_the_chooser(self):
        r = self.client.get(reverse("demo:index"))
        self.assertEqual(r.status_code, 302)
        # The chooser, not a domain: the demo may span several domains and
        # picking one for the visitor hid the rest.
        self.assertEqual(r["Location"], reverse("home"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.guest.pk)

    def test_demo_index_404_without_demo_content(self):
        self.grant.delete()
        self.assertEqual(self.client.get(reverse("demo:index")).status_code, 404)

    def test_guest_file_read_is_scoped_to_granted_projects(self):
        """The narrowing holds at the HTTP layer, not only in predicates.

        ``/demo/`` logs an anonymous visitor in as a *real* user, so
        ``@login_required`` alone scopes nothing at all -- the refusal has to come
        from project access. Every endpoint that streams a ``FileRegistry`` row
        funnels through ``common.file_access.authorize_file_read``, so asserting
        it here asserts it for all of them.
        """
        self.client.force_login(self.guest)
        denied = self.client.get(
            reverse("api:api_serve_file", args=[self.priv_file.id])
        )
        self.assertIn(denied.status_code, (403, 404))
        # And the narrowing is a narrowing, not a blanket refusal: the granted
        # project's own file is not blocked by the permission layer. It 404s on
        # the missing object instead, a storage answer and not an authorization one.
        allowed = self.client.get(
            reverse("api:api_serve_file", args=[self.demo_file.id])
        )
        self.assertNotEqual(allowed.status_code, 403)

    def test_guest_write_request_forbidden(self):
        self.client.force_login(self.guest)
        # Any non-safe method by the guest is rejected before the view runs.
        self.assertEqual(self.client.post("/maxillo/patient/1/").status_code, 403)

    def test_normal_user_write_not_blocked_by_demo_middleware(self):
        # A non-guest POST is not short-circuited to 403 by the demo middleware
        # (it may 404/redirect for other reasons, just never the demo backstop).
        self.client.force_login(self.normal)
        self.assertNotEqual(self.client.post("/demo/").status_code, 403)


class LandingDomainCardTests(TestCase):
    """The chooser must not offer a domain the user cannot enter."""

    @classmethod
    def setUpTestData(cls):
        cls.maxillo = Project.objects.create(
            name="Maxillo demo", slug="maxillo-demo", domain="maxillo", is_active=True
        )
        cls.brain = Project.objects.create(
            name="Brain demo", slug="brain-demo", domain="brain", is_active=True
        )
        User = get_user_model()
        cls.user = User.objects.create_user(username="bob", password="x")
        ProjectAccess.objects.create(user=cls.user, project=cls.brain, role="viewer")

    def test_cards_limited_to_accessible_domains(self):
        from common.domains import landing_domain_cards

        slugs = [c["slug"] for c in landing_domain_cards(self.user)]
        self.assertEqual(slugs, ["brain"])

    def test_staff_sees_every_domain(self):
        from common.domains import landing_domain_cards

        User = get_user_model()
        staff = User.objects.create_user(username="root", password="x", is_staff=True)
        slugs = [c["slug"] for c in landing_domain_cards(staff)]
        self.assertEqual(slugs, ["maxillo", "brain", "laparoscopy"])

    def test_anonymous_gets_no_cards(self):
        from django.contrib.auth.models import AnonymousUser

        from common.domains import landing_domain_cards

        self.assertEqual(landing_domain_cards(AnonymousUser()), [])
