from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from common.models import Project, ProjectAccess
from laparoscopy.models import Patient


class LaparoscopyUploadValidationTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.get_or_create(slug="laparoscopy", defaults={"name": "Laparoscopy"})
        self.user = User.objects.create_user(username="laparoscopy-upload-admin", password="x")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.client.force_login(self.user)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

    def test_empty_upload_does_not_create_patient(self):
        response = self.client.post(reverse("laparoscopy:upload_patient"), {"name": "Empty video"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add at least one file before uploading.")
        self.assertFalse(Patient.objects.filter(name="Empty video").exists())
