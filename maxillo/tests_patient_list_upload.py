from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from annotations.services.ios_landmarks import save_ios_landmarks
from common.models import AnnotationMethod, FileRegistry, Project, ProjectAccess
from maxillo.models import Classification, Folder, Patient


class MaxilloPatientListFiltersTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.get_or_create(slug="maxillo", defaults={"name": "Maxillo"})
        self.user = User.objects.create_user(username="maxillo-list-admin", password="x")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.folder = Folder.objects.create(name="General", project=self.project)
        self.client.force_login(self.user)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

    def test_defaults_to_ten_rows_without_count_card_or_avatar_controls(self):
        Patient.objects.bulk_create(
            [Patient(name=f"Patient {index}", project=self.project, folder=self.folder) for index in range(11)]
        )

        response = self.client.get(reverse("maxillo:patient_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["per_page"], 10)
        self.assertEqual(len(response.context["page_obj"].object_list), 10)
        self.assertNotContains(response, "view-toggle")
        self.assertNotContains(response, "patient-avatar")
        self.assertNotContains(response, "10 of 11 patients")

    def test_bite_classification_and_landmark_presence_filters(self):
        classified = Patient.objects.create(name="Classified", project=self.project, folder=self.folder)
        landmarked = Patient.objects.create(name="Landmarked", project=self.project, folder=self.folder)
        Patient.objects.create(name="Neither", project=self.project, folder=self.folder)
        Classification.objects.create(
            patient=classified,
            classifier="manual",
            sagittal_left="I",
            sagittal_right="I",
            vertical="normal",
            transverse="normal",
            midline="centered",
        )
        # Landmarks live in `annotations/` now (decision #20), so presence is a question
        # about the record rather than about an object in storage. The two differ for a
        # patient whose every landmark was deleted: the file row survives as history and
        # the filter should not find it.
        self.project.annotation_methods.add(
            AnnotationMethod.objects.get_or_create(
                slug="ios_landmarks", defaults={"name": "IOS Landmarks"}
            )[0]
        )
        meshes = {
            jaw: FileRegistry.objects.create(
                patient=landmarked,
                domain="maxillo",
                file_type=f"ios_raw_{jaw}",
                file_path=f"maxillo/ios/{jaw}.stl",
                file_size=2,
                file_hash=str(index) * 64,
            )
            for index, jaw in enumerate(("upper", "lower"))
        }
        save_ios_landmarks(
            landmarked,
            meshes=[
                {
                    "file_obj": meshes["upper"],
                    "jaw": "upper",
                    "landmarks": {"11": {"incisal": [1.0, 2.0, 3.0]}},
                }
            ],
        )

        bite_response = self.client.get(reverse("maxillo:patient_list"), {"has_bite_classification": "yes"})
        landmark_response = self.client.get(reverse("maxillo:patient_list"), {"has_landmarks": "yes"})

        self.assertEqual([item["patient"] for item in bite_response.context["page_obj"].object_list], [classified])
        self.assertEqual([item["patient"] for item in landmark_response.context["page_obj"].object_list], [landmarked])

    def test_invalid_page_size_falls_back_to_ten(self):
        response = self.client.get(reverse("maxillo:patient_list"), {"per_page": "invalid"})
        self.assertEqual(response.context["per_page"], 10)


class MaxilloUploadValidationTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.get_or_create(slug="maxillo", defaults={"name": "Maxillo", "domain": "maxillo"})
        self.user = User.objects.create_user(username="maxillo-upload-admin", password="x")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.folder = Folder.objects.create(name="General", project=self.project)
        self.client.force_login(self.user)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

    def test_empty_upload_does_not_create_patient(self):
        response = self.client.post(
            reverse("maxillo:upload_patient"),
            {"name": "Empty", "project": str(self.project.id), "folder": str(self.folder.id)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add at least one file before uploading.")
        self.assertFalse(Patient.objects.filter(name="Empty").exists())
