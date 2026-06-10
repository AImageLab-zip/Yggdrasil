import json

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from common.models import Job, Project, ProjectAccess
from common.permissions import (
    filter_patients_for_user,
    user_can_delete_caption,
    user_can_delete_single_patient,
    user_can_edit_caption,
    user_can_edit_metadata,
    user_can_move_patient,
    user_can_perform_bulk_operations,
    user_can_read_folder,
    user_can_view_caption_content,
    user_can_write_annotations,
)
from maxillo.models import Folder, FolderAccess, Patient, VoiceCaption


class MaxilloFolderAclTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.get_or_create(name="maxillo", defaults={"slug": "maxillo"})
        self.admin = User.objects.create_user(username="admin", password="x")
        self.user = User.objects.create_user(username="user", password="x")
        self.other = User.objects.create_user(username="other", password="x")

        ProjectAccess.objects.create(user=self.admin, project=self.project, role="admin")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="standard")
        ProjectAccess.objects.create(user=self.other, project=self.project, role="standard")

        self.folder = Folder.objects.create(name="F1")
        self.patient = Patient.objects.create(name="P1", folder=self.folder)

    def test_admin_sees_all_patients(self):
        qs = filter_patients_for_user(self.admin, Patient.objects.all(), "maxillo")
        self.assertEqual(qs.count(), 1)

    def test_non_admin_without_folder_access_sees_nothing(self):
        qs = filter_patients_for_user(self.user, Patient.objects.all(), "maxillo")
        self.assertEqual(qs.count(), 0)

    def test_standard_role_read_only(self):
        FolderAccess.objects.create(user=self.user, folder=self.folder, role="standard")
        self.assertTrue(user_can_read_folder(self.user, self.folder, "maxillo"))
        self.assertFalse(user_can_write_annotations(self.user, self.folder, "maxillo"))

    def test_annotator_can_write_and_delete_single(self):
        FolderAccess.objects.create(user=self.user, folder=self.folder, role="annotator")
        self.assertTrue(user_can_write_annotations(self.user, self.folder, "maxillo"))
        self.assertTrue(user_can_delete_single_patient(self.user, self.folder, "maxillo"))

    def test_project_manager_matches_annotator(self):
        FolderAccess.objects.create(user=self.user, folder=self.folder, role="project_manager")
        self.assertTrue(user_can_write_annotations(self.user, self.folder, "maxillo"))
        self.assertTrue(user_can_delete_single_patient(self.user, self.folder, "maxillo"))

    def test_move_and_bulk_admin_only(self):
        FolderAccess.objects.create(user=self.user, folder=self.folder, role="project_manager")
        self.assertFalse(user_can_move_patient(self.user, self.patient))
        self.assertFalse(user_can_perform_bulk_operations(self.user, "maxillo"))
        self.assertTrue(user_can_move_patient(self.admin, self.patient))
        self.assertTrue(user_can_perform_bulk_operations(self.admin, "maxillo"))

    def test_metadata_admin_only(self):
        FolderAccess.objects.create(user=self.user, folder=self.folder, role="project_manager")
        self.assertFalse(user_can_edit_metadata(self.user, self.patient))
        self.assertTrue(user_can_edit_metadata(self.admin, self.patient))

    def test_caption_owner_or_admin(self):
        caption = VoiceCaption.objects.create(patient=self.patient, user=self.user, modality="audio", duration=1.0)
        self.assertTrue(user_can_edit_caption(self.user, caption))
        self.assertFalse(user_can_edit_caption(self.other, caption))
        self.assertTrue(user_can_edit_caption(self.admin, caption))
        self.assertTrue(user_can_delete_caption(self.admin, caption))

    def test_caption_content_visibility_by_folder_role(self):
        owner = User.objects.create_user(username="caption_owner", password="x")
        standard = User.objects.create_user(username="caption_standard", password="x")
        annotator = User.objects.create_user(username="caption_annotator", password="x")
        project_manager = User.objects.create_user(username="caption_pm", password="x")
        outsider = User.objects.create_user(username="caption_outsider", password="x")

        ProjectAccess.objects.create(user=owner, project=self.project, role="standard")
        ProjectAccess.objects.create(user=standard, project=self.project, role="standard")
        ProjectAccess.objects.create(user=annotator, project=self.project, role="standard")
        ProjectAccess.objects.create(user=project_manager, project=self.project, role="standard")
        ProjectAccess.objects.create(user=outsider, project=self.project, role="standard")

        FolderAccess.objects.create(user=owner, folder=self.folder, role="annotator")
        FolderAccess.objects.create(user=standard, folder=self.folder, role="standard")
        FolderAccess.objects.create(user=annotator, folder=self.folder, role="annotator")
        FolderAccess.objects.create(user=project_manager, folder=self.folder, role="project_manager")

        caption = VoiceCaption.objects.create(patient=self.patient, user=owner, modality="audio", duration=1.0)

        self.assertTrue(user_can_view_caption_content(owner, caption, "maxillo"))
        self.assertTrue(user_can_view_caption_content(self.admin, caption, "maxillo"))
        self.assertTrue(user_can_view_caption_content(standard, caption, "maxillo"))
        self.assertTrue(user_can_view_caption_content(project_manager, caption, "maxillo"))
        self.assertFalse(user_can_view_caption_content(annotator, caption, "maxillo"))
        self.assertFalse(user_can_view_caption_content(outsider, caption, "maxillo"))

    def test_standard_role_cannot_create_text_caption(self):
        FolderAccess.objects.create(user=self.user, folder=self.folder, role="standard")
        self.client.force_login(self.user)

        response = self.client.post(
            reverse(
                "maxillo:upload_text_caption",
                kwargs={"patient_id": self.patient.patient_id},
            ),
            data=json.dumps({"text": "A read-only user should not be able to save this caption."}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(VoiceCaption.objects.count(), 0)

    def test_annotator_role_can_create_text_caption(self):
        FolderAccess.objects.create(user=self.user, folder=self.folder, role="annotator")
        self.client.force_login(self.user)

        response = self.client.post(
            reverse(
                "maxillo:upload_text_caption",
                kwargs={"patient_id": self.patient.patient_id},
            ),
            data=json.dumps({"text": "An annotator can save this caption."}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(VoiceCaption.objects.count(), 1)


class MaxilloJobApiAclTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.get_or_create(
            name="maxillo", defaults={"slug": "maxillo"}
        )
        self.admin = User.objects.create_user(username="job_admin", password="x")
        self.user = User.objects.create_user(username="job_user", password="x")
        self.other = User.objects.create_user(username="job_other", password="x")

        ProjectAccess.objects.create(user=self.admin, project=self.project, role="admin")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="standard")
        ProjectAccess.objects.create(user=self.other, project=self.project, role="standard")

        self.folder_allowed = Folder.objects.create(name="F-allowed")
        self.folder_denied = Folder.objects.create(name="F-denied")
        self.patient_allowed = Patient.objects.create(name="P-allowed", folder=self.folder_allowed)
        self.patient_denied = Patient.objects.create(name="P-denied", folder=self.folder_denied)

        FolderAccess.objects.create(user=self.user, folder=self.folder_allowed, role="standard")

        self.allowed_job = Job.objects.create(
            domain="maxillo", modality_slug="cbct", patient=self.patient_allowed
        )
        self.denied_job = Job.objects.create(
            domain="maxillo", modality_slug="cbct", patient=self.patient_denied
        )

    def test_job_endpoints_require_login(self):
        response = self.client.get(reverse("maxillo:api_processing_jobs"))
        self.assertEqual(response.status_code, 302)

    def test_job_list_is_folder_filtered_for_non_admin(self):
        self.client.login(username="job_user", password="x")
        response = self.client.get(reverse("maxillo:api_processing_jobs"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body.get("success"))
        self.assertEqual(len(body.get("jobs", [])), 1)
        self.assertEqual(body["jobs"][0]["id"], self.allowed_job.id)

    def test_job_status_denies_unassigned_folder_user(self):
        self.client.login(username="job_other", password="x")
        response = self.client.get(
            reverse("maxillo:api_get_job_status", kwargs={"job_id": self.allowed_job.id})
        )
        self.assertEqual(response.status_code, 403)
