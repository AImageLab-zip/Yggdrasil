"""Guest-demo isolation tests (Phase 7).

These lock the security invariant: the anonymous /demo/ routes expose only
patients (and their files) that live in an ``is_demo`` folder, and nothing
else — no login, no write methods.
"""

from django.apps import apps
from django.test import TestCase
from django.urls import reverse

from common.demo import demo_patients, landing_demo_url, patient_in_demo
from common.models import FileRegistry


class DemoIsolationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Folder = apps.get_model("maxillo", "Folder")
        Patient = apps.get_model("maxillo", "Patient")
        cls.demo_folder = Folder.objects.create(name="Demo", is_demo=True)
        cls.priv_folder = Folder.objects.create(name="Private", is_demo=False)
        cls.demo_patient = Patient.objects.create(
            name="Demo Pt", folder=cls.demo_folder, visibility="public"
        )
        cls.priv_patient = Patient.objects.create(
            name="Private Pt", folder=cls.priv_folder, visibility="private"
        )
        cls.demo_file = FileRegistry.objects.create(
            patient=cls.demo_patient, file_path="raw/demo/x.png",
            file_type="rgb_image", domain="maxillo", file_size=1, file_hash="d",
        )
        cls.priv_file = FileRegistry.objects.create(
            patient=cls.priv_patient, file_path="raw/priv/y.png",
            file_type="rgb_image", domain="maxillo", file_size=1, file_hash="p",
        )

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

    # --- HTTP layer (anonymous) ---
    def test_index_and_list_anonymous_ok(self):
        self.assertEqual(self.client.get(reverse("demo:index")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("demo:domain", args=["maxillo"])).status_code, 200
        )

    def test_demo_patient_detail_ok(self):
        r = self.client.get(reverse("demo:patient", args=["maxillo", self.demo_patient.pk]))
        self.assertEqual(r.status_code, 200)

    def test_private_patient_detail_404(self):
        r = self.client.get(reverse("demo:patient", args=["maxillo", self.priv_patient.pk]))
        self.assertEqual(r.status_code, 404)

    def test_private_file_404(self):
        r = self.client.get(reverse("demo:file", args=["maxillo", self.priv_file.id]))
        self.assertEqual(r.status_code, 404)

    def test_write_methods_rejected(self):
        self.assertEqual(self.client.post(reverse("demo:index")).status_code, 405)

    def test_unknown_domain_404(self):
        self.assertEqual(self.client.get("/demo/nope/").status_code, 404)
