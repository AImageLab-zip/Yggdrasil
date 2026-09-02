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
from maxillo.models import Folder, Patient, VoiceCaption


class MaxilloProjectAclTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.get_or_create(
            name="maxillo", defaults={"slug": "maxillo", "domain": "maxillo"}
        )
        self.admin = User.objects.create_user(username="admin", password="x")
        self.viewer = User.objects.create_user(username="viewer", password="x")
        self.annotator = User.objects.create_user(username="annotator", password="x")
        self.other = User.objects.create_user(username="other", password="x")

        ProjectAccess.objects.create(user=self.admin, project=self.project, role="admin")
        ProjectAccess.objects.create(user=self.viewer, project=self.project, role="viewer")
        ProjectAccess.objects.create(user=self.annotator, project=self.project, role="annotator")

        self.folder = Folder.objects.create(name="F1", project=self.project)
        self.patient = Patient.objects.create(
            name="P1", folder=self.folder, project=self.project
        )

    def test_admin_sees_all_patients(self):
        qs = filter_patients_for_user(self.admin, Patient.objects.all(), "maxillo")
        self.assertEqual(qs.count(), 1)

    def test_user_without_project_access_sees_nothing(self):
        qs = filter_patients_for_user(self.other, Patient.objects.all(), "maxillo")
        self.assertEqual(qs.count(), 0)

    def test_project_member_sees_patient(self):
        qs = filter_patients_for_user(self.viewer, Patient.objects.all(), "maxillo")
        self.assertEqual(list(qs), [self.patient])

    def test_viewer_role_read_only(self):
        self.assertTrue(user_can_read_folder(self.viewer, self.folder, self.project))
        self.assertFalse(user_can_write_annotations(self.viewer, self.folder, self.project))

    def test_annotator_can_write_and_delete_single(self):
        self.assertTrue(user_can_write_annotations(self.annotator, self.folder, self.project))
        self.assertTrue(user_can_delete_single_patient(self.annotator, self.folder, self.project))

    def test_admin_matches_annotator_plus_more(self):
        self.assertTrue(user_can_write_annotations(self.admin, self.folder, self.project))
        self.assertTrue(user_can_delete_single_patient(self.admin, self.folder, self.project))
        self.assertTrue(user_can_move_patient(self.admin, self.patient))
        self.assertTrue(user_can_perform_bulk_operations(self.admin, self.project))

    def test_move_and_bulk_admin_only(self):
        self.assertFalse(user_can_move_patient(self.annotator, self.patient))
        self.assertFalse(user_can_perform_bulk_operations(self.annotator, self.project))
        self.assertFalse(user_can_move_patient(self.viewer, self.patient))

    def test_metadata_admin_only(self):
        self.assertFalse(user_can_edit_metadata(self.annotator, self.patient))
        self.assertFalse(user_can_edit_metadata(self.viewer, self.patient))
        self.assertTrue(user_can_edit_metadata(self.admin, self.patient))

    def test_caption_owner_or_admin(self):
        caption = VoiceCaption.objects.create(patient=self.patient, user=self.viewer, modality="audio", duration=1.0)
        self.assertTrue(user_can_edit_caption(self.viewer, caption))
        self.assertFalse(user_can_edit_caption(self.annotator, caption))
        self.assertTrue(user_can_edit_caption(self.admin, caption))
        self.assertTrue(user_can_delete_caption(self.admin, caption))

    def test_caption_content_visibility_by_project_role(self):
        owner = User.objects.create_user(username="caption_owner", password="x")
        standard = User.objects.create_user(username="caption_viewer", password="x")
        annotator = User.objects.create_user(username="caption_annotator", password="x")
        pm = User.objects.create_user(username="caption_admin", password="x")
        outsider = User.objects.create_user(username="caption_outsider", password="x")

        ProjectAccess.objects.create(user=owner, project=self.project, role="viewer")
        ProjectAccess.objects.create(user=standard, project=self.project, role="viewer")
        ProjectAccess.objects.create(user=annotator, project=self.project, role="annotator")
        ProjectAccess.objects.create(user=pm, project=self.project, role="admin")

        caption = VoiceCaption.objects.create(patient=self.patient, user=owner, modality="audio", duration=1.0)

        # Owner always sees their own content.
        self.assertTrue(user_can_view_caption_content(owner, caption, self.project))
        # Admin and viewers see all content.
        self.assertTrue(user_can_view_caption_content(self.admin, caption, self.project))
        self.assertTrue(user_can_view_caption_content(standard, caption, self.project))
        self.assertTrue(user_can_view_caption_content(pm, caption, self.project))
        # Annotators see only their own captions (bias guard), outsiders nothing.
        self.assertFalse(user_can_view_caption_content(annotator, caption, self.project))
        self.assertFalse(user_can_view_caption_content(outsider, caption, self.project))

    def test_caption_visibility_does_not_depend_on_the_patient_having_a_folder(self):
        """An unfiled patient's captions stay visible to the project's viewers.

        The patient detail view used to derive the role from ``patient.folder``,
        so a patient with no folder -- ``Patient.folder`` is SET_NULL, and a
        deleted folder unfiles every patient in it -- had no role at all and its
        captions were ghosted for the project's own viewers.
        """
        viewer = User.objects.create_user(username="unfiled_viewer", password="x")
        ProjectAccess.objects.create(user=viewer, project=self.project, role="viewer")
        owner = User.objects.create_user(username="unfiled_owner", password="x")

        self.patient.folder = None
        self.patient.save(update_fields=["folder"])
        caption = VoiceCaption.objects.create(
            patient=self.patient, user=owner, modality="audio", duration=1.0
        )

        self.assertTrue(user_can_view_caption_content(viewer, caption))

        # And through the view, which is where the folder-derived role lived.
        self.client.force_login(viewer)
        response = self.client.get(
            reverse("maxillo:patient_detail", args=[self.patient.patient_id])
        )
        self.assertEqual(response.status_code, 200)
        rendered = response.context["voice_captions"]
        self.assertTrue(all(c.can_view_content for c in rendered))

    def test_viewer_role_cannot_create_text_caption(self):
        self.client.force_login(self.viewer)

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
        self.client.force_login(self.annotator)

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
            name="maxillo", defaults={"slug": "maxillo", "domain": "maxillo"}
        )
        self.admin = User.objects.create_user(username="job_admin", password="x")
        self.user = User.objects.create_user(username="job_user", password="x")
        self.other = User.objects.create_user(username="job_other", password="x")

        ProjectAccess.objects.create(user=self.admin, project=self.project, role="admin")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="viewer")

        self.folder = Folder.objects.create(name="F", project=self.project)
        self.patient = Patient.objects.create(
            name="P", folder=self.folder, project=self.project
        )
        self.job = Job.objects.create(
            domain="maxillo", modality_slug="cbct", patient=self.patient
        )

    def test_job_endpoints_require_login(self):
        response = self.client.get(reverse("maxillo:api_processing_jobs"))
        self.assertEqual(response.status_code, 302)

    def test_job_list_is_project_filtered_for_member(self):
        self.client.login(username="job_user", password="x")
        response = self.client.get(reverse("maxillo:api_processing_jobs"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body.get("success"))
        self.assertEqual(len(body.get("jobs", [])), 1)
        self.assertEqual(body["jobs"][0]["id"], self.job.id)

    def test_job_status_denies_user_without_project_access(self):
        self.client.login(username="job_other", password="x")
        response = self.client.get(
            reverse("maxillo:api_get_job_status", kwargs={"job_id": self.job.id})
        )
        # Users without ProjectAccess to the domain are bounced by the profile
        # middleware (redirect to home) before any view runs.
        self.assertEqual(response.status_code, 302)
