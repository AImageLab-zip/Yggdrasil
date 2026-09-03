from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from common.models import FileRegistry, Modality, ProcessingStep, Project, ProjectAccess
from maxillo.models import Patient


class IOSViewerFilePreferenceTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Maxillo viewer", slug="maxillo-viewer", domain="maxillo"
        )
        self.user = User.objects.create_user(username="viewer-admin", password="x")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.client.force_login(self.user)
        self.patient = Patient.objects.create(
            name="Viewer Patient", project=self.project
        )
        self.modality = Modality.objects.create(name="IOS", slug="ios")
        self.step = ProcessingStep.objects.create(
            modality=self.modality,
            name="IOS",
            slug="ios",
            is_blocking=False,
        )

    def _file(self, file_type, name, subtype=""):
        return FileRegistry.objects.create(
            file_type=file_type,
            file_path=f"maxillo/{name}",
            file_size=1,
            file_hash=name,
            patient=self.patient,
            modality=self.modality,
            subtype=subtype,
        )

    def _raw_pair(self):
        return (
            self._file("ios_raw_upper", "raw-upper.stl"),
            self._file("ios_raw_lower", "raw-lower.stl"),
        )

    def _processed_pair(self, legacy=False):
        if legacy:
            return (
                self._file("ios_processed_upper", "processed-upper.stl"),
                self._file("ios_processed_lower", "processed-lower.stl"),
            )
        return (
            self._file("ios_processed", "processed-upper.stl", "upper"),
            self._file("ios_processed", "processed-lower.stl", "lower"),
        )

    def _viewer_data(self):
        return self.client.get(
            reverse(
                "maxillo:patient_viewer_data",
                kwargs={"patient_id": self.patient.patient_id},
            )
        )

    def _assert_pair(self, response, pair):
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(
            body["upper_scan_url"].endswith(
                reverse("maxillo:api_serve_file", kwargs={"file_id": pair[0].id})
            )
        )
        self.assertTrue(
            body["lower_scan_url"].endswith(
                reverse("maxillo:api_serve_file", kwargs={"file_id": pair[1].id})
            )
        )

    def test_raw_pair_is_preferred_by_default(self):
        raw_pair = self._raw_pair()
        self._processed_pair()

        self._assert_pair(self._viewer_data(), raw_pair)

    def test_checked_option_prefers_processed_pair(self):
        self._raw_pair()
        processed_pair = self._processed_pair(legacy=True)
        self.step.prefer_processed_for_viewer = True
        self.step.save()

        self.assertTrue(self.patient.has_ios_scans())
        self._assert_pair(self._viewer_data(), processed_pair)

    def test_checked_option_falls_back_to_complete_raw_pair(self):
        raw_pair = self._raw_pair()
        self._file("ios_processed", "processed-upper.stl", "upper")
        self.step.prefer_processed_for_viewer = True
        self.step.save()

        self._assert_pair(self._viewer_data(), raw_pair)

    def test_unchecked_option_falls_back_to_processed_pair(self):
        processed_pair = self._processed_pair()

        self._assert_pair(self._viewer_data(), processed_pair)

    def test_runner_output_filenames_are_recognized_as_processed_arches(self):
        processed_pair = (
            self._file("ios_processed", "oriented-upper.stl", "upper_oriented.stl"),
            self._file("ios_processed", "oriented-lower.stl", "lower_oriented.stl"),
        )
        self.step.prefer_processed_for_viewer = True
        self.step.save()

        self._assert_pair(self._viewer_data(), processed_pair)

    def test_discard_raw_forces_processed_pair(self):
        self._raw_pair()
        processed_pair = self._processed_pair()
        self.step.discard_raw = True
        self.step.save()

        self._assert_pair(self._viewer_data(), processed_pair)

    def test_discarded_ios_raw_file_cannot_be_downloaded(self):
        raw_pair = self._raw_pair()
        self.step.discard_raw = True
        self.step.save()

        response = self.client.get(
            reverse("maxillo:api_serve_file", kwargs={"file_id": raw_pair[0].id})
        )

        self.assertEqual(response.status_code, 404)
