from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from brain.models import Folder, Patient
from common.models import FileRegistry, Modality, Project, ProjectAccess


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
