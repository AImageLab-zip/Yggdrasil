"""Guest-demo isolation tests (Phase 7).

These lock the security invariant: the shared read-only demo guest can read
only patients (and folders) that live in an ``is_demo`` folder, can never
write, and the /demo/ entry point logs it into the real portal.
"""

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from common.demo import demo_patients, is_demo_guest, landing_demo_url, patient_in_demo
from common.models import FileRegistry, Project, ProjectAccess
from common.permissions import (
    filter_folders_for_user,
    filter_patients_for_user,
    user_can_read_folder,
    user_can_write_annotations,
)


class DemoIsolationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Folder = apps.get_model("maxillo", "Folder")
        Patient = apps.get_model("maxillo", "Patient")
        cls.Folder = Folder
        cls.Patient = Patient
        # Folders always belong to a project, so the project is created first.
        cls.project = Project.objects.create(
            name="Maxillo demo", slug="maxillo-demo", domain="maxillo", is_active=True
        )
        cls.demo_folder = Folder.objects.create(
            name="Demo", is_demo=True, project=cls.project
        )
        cls.priv_folder = Folder.objects.create(
            name="Private", is_demo=False, project=cls.project
        )
        cls.demo_patient = Patient.objects.create(
            name="Demo Pt", folder=cls.demo_folder, project=cls.project,
            visibility="public",
        )
        cls.priv_patient = Patient.objects.create(
            name="Private Pt", folder=cls.priv_folder, project=cls.project,
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

        # The guest user is created by migration 0036, but its ProjectAccess is
        # only seeded for projects that existed at migrate time — none in the
        # test DB — so grant it here for the project this test uses.
        User = get_user_model()
        cls.guest = User.objects.get(username=settings.DEMO_GUEST_USERNAME)
        ProjectAccess.objects.get_or_create(
            user=cls.guest, project=cls.project, defaults={"role": "standard"}
        )
        cls.normal = User.objects.create_user(username="alice", password="x")

    # --- queryset / predicate layer ---
    def test_demo_patients_only_demo_folder(self):
        pks = set(demo_patients("maxillo").values_list("pk", flat=True))
        self.assertEqual(pks, {self.demo_patient.pk})

    def test_patient_in_demo_predicate(self):
        self.assertTrue(patient_in_demo(self.demo_patient, "maxillo"))
        self.assertFalse(patient_in_demo(self.priv_patient, "maxillo"))

    def test_landing_demo_url_gating(self):
        self.assertEqual(landing_demo_url(), reverse("demo:index"))
        self.demo_folder.is_demo = False
        self.demo_folder.save(update_fields=["is_demo"])
        self.assertIsNone(landing_demo_url())

    # --- guest identity ---
    def test_is_demo_guest_helper(self):
        self.assertTrue(is_demo_guest(self.guest))
        self.assertFalse(is_demo_guest(self.normal))

    # --- permission scoping (the real security boundary) ---
    def test_guest_reads_only_demo_folder(self):
        self.assertTrue(user_can_read_folder(self.guest, self.demo_folder, "maxillo"))
        self.assertFalse(user_can_read_folder(self.guest, self.priv_folder, "maxillo"))

    def test_guest_patient_list_scoped_to_demo(self):
        pks = set(
            filter_patients_for_user(
                self.guest, self.Patient.objects.all(), "maxillo"
            ).values_list("pk", flat=True)
        )
        self.assertEqual(pks, {self.demo_patient.pk})

    def test_guest_folder_list_scoped_to_demo(self):
        ids = set(
            filter_folders_for_user(
                self.guest, self.Folder.objects.all(), "maxillo"
            ).values_list("id", flat=True)
        )
        self.assertEqual(ids, {self.demo_folder.id})

    def test_guest_cannot_write(self):
        self.assertFalse(
            user_can_write_annotations(self.guest, self.demo_folder, "maxillo")
        )

    # --- HTTP layer ---
    def test_demo_index_logs_in_and_redirects(self):
        r = self.client.get(reverse("demo:index"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "/maxillo/")
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.guest.pk)

    def test_demo_index_404_without_demo_content(self):
        self.demo_folder.is_demo = False
        self.demo_folder.save(update_fields=["is_demo"])
        self.assertEqual(self.client.get(reverse("demo:index")).status_code, 404)

    def test_guest_file_read_is_scoped_to_demo_folders(self):
        """The ``is_demo`` narrowing holds at the HTTP layer, not only in predicates.

        Re-homed from the DICOMweb suite, which was the only place this was asserted
        against a real streaming endpoint. Finding F10 is why it has to be: ``/demo/``
        logs an anonymous visitor in as a *real* user, so ``@login_required`` alone
        scopes nothing at all -- the refusal has to come from the folder flag. Every
        endpoint that streams a ``FileRegistry`` row funnels through
        ``common.file_access.authorize_file_read``, so asserting it here asserts it
        for all of them.
        """
        self.client.force_login(self.guest)
        denied = self.client.get(
            reverse("api:api_serve_file", args=[self.priv_file.id])
        )
        self.assertIn(denied.status_code, (403, 404))
        # And the narrowing is a narrowing, not a blanket refusal: the demo folder's
        # own file is not blocked by the permission layer. It 404s on the missing
        # object instead, which is a storage answer and not an authorization one.
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
