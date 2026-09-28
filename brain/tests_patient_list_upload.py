from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from brain.models import Folder, Patient
from common.models import FileRegistry, Job, Modality, Project, ProjectAccess


class BrainPatientListTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.get_or_create(slug="brain", defaults={"name": "Brain", "domain": "brain"})
        self.modality = Modality.objects.create(name="Brain MRI T1", slug="braintumor-mri-t1")
        self.project.modalities.add(self.modality)
        self.folder = Folder.objects.create(name="General", project=self.project)
        self.user = User.objects.create_user(username="brain-list-admin", password="x")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.client.force_login(self.user)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

    def test_defaults_to_ten_and_applies_modality_status_filter(self):
        patients = [
            Patient.objects.create(name=f"Brain {index}", project=self.project, folder=self.folder)
            for index in range(11)
        ]
        FileRegistry.objects.create(
            brain_patient=patients[0],
            domain="brain",
            modality=self.modality,
            file_type="braintumor_mri_t1_raw",
            file_path="brain/tests/t1.nii.gz",
            file_size=2,
            file_hash="1" * 64,
        )

        response = self.client.get(reverse("brain:patient_list"))
        filtered = self.client.get(reverse("brain:patient_list"), {"status_braintumor-mri-t1": "processed"})

        self.assertEqual(response.context["per_page"], 10)
        self.assertEqual(len(response.context["page_obj"].object_list), 10)
        self.assertEqual([item["patient"] for item in filtered.context["page_obj"].object_list], [patients[0]])

    def _add_patients(self, count, processed=False):
        patients = []
        for index in range(count):
            patient = Patient.objects.create(name=f"Brain extra {index}", project=self.project, folder=self.folder)
            if processed:
                FileRegistry.objects.create(
                    brain_patient=patient,
                    domain="brain",
                    modality=self.modality,
                    file_type="braintumor_mri_t1_raw",
                    file_path=f"brain/tests/{patient.pk}/t1.nii.gz",
                    file_size=2,
                    file_hash=f"{patient.pk:064d}",
                )
                Job.objects.create(
                    domain="brain",
                    brain_patient=patient,
                    modality_slug=self.modality.slug,
                    input_files={"input": f"brain/tests/{patient.pk}/t1.nii.gz"},
                    status="completed",
                )
            patients.append(patient)
        return patients

    def _queries_for(self, params=None):
        # The first request of a test also saves the session; keep that out of the count.
        self.client.get(reverse("brain:patient_list"), params or {})
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("brain:patient_list"), params or {})
        self.assertEqual(response.status_code, 200)
        return len(captured.captured_queries)

    def test_page_cost_does_not_grow_with_the_project(self):
        """#97: rows used to be built for every patient before paginating."""
        self._add_patients(10, processed=True)
        small = self._queries_for()
        self._add_patients(30, processed=True)
        self.assertEqual(self._queries_for(), small)

    def test_status_filter_page_cost_does_not_grow_with_the_project(self):
        """The filtered path still reads every candidate, but builds rows for the page only."""
        params = {"status_braintumor-mri-t1": "processed"}
        self._add_patients(10, processed=True)
        small = self._queries_for(params)
        self._add_patients(30, processed=True)
        self.assertEqual(self._queries_for(params), small)

    def test_second_page_without_filters(self):
        self._add_patients(11)
        response = self.client.get(reverse("brain:patient_list"), {"page": 2})
        page_obj = response.context["page_obj"]
        self.assertEqual(page_obj.number, 2)
        self.assertEqual(page_obj.paginator.num_pages, 2)
        self.assertEqual(page_obj.paginator.count, 11)
        self.assertEqual(len(page_obj.object_list), 1)
        self.assertIn("modality_status_list", page_obj.object_list[0])

    def test_status_filter_paginates_the_matches(self):
        processed = self._add_patients(12, processed=True)
        self._add_patients(5)
        response = self.client.get(
            reverse("brain:patient_list"), {"status_braintumor-mri-t1": "processed", "page": 2}
        )
        page_obj = response.context["page_obj"]
        self.assertEqual(page_obj.paginator.count, 12)
        self.assertEqual(len(page_obj.object_list), 2)
        for item in page_obj.object_list:
            self.assertIn(item["patient"], processed)
            self.assertEqual(item["modality_statuses"]["braintumor-mri-t1"], "processed")

    def test_invalid_page_size_falls_back_to_ten(self):
        response = self.client.get(reverse("brain:patient_list"), {"per_page": "200"})
        self.assertEqual(response.context["per_page"], 10)

    def test_empty_upload_does_not_create_patient(self):
        response = self.client.post(
            reverse("brain:upload_patient"),
            {"name": "Empty brain", "project": str(self.project.id), "folder": str(self.folder.id)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add at least one file before uploading.")
        self.assertFalse(Patient.objects.filter(name="Empty brain").exists())

    def test_xhr_upload_answers_in_json(self):
        """The uploader navigates on JSON and treats anything else as failure.

        `static/js/cbct_upload.js` posts with this header and reads
        `{"ok": true, "redirect": ...}`. Brain used to answer with a 302 the XHR
        followed to the patient-list HTML, so a *successful* upload surfaced as
        "Upload failed (HTTP 200)" and the page never moved.
        """
        response = self.client.post(
            reverse("brain:upload_patient"),
            {"name": "Empty brain", "project": str(self.project.id), "folder": str(self.folder.id)},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response["Content-Type"], "application/json")
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertIn("Add at least one file", payload["error"])
