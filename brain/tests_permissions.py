from django.contrib.auth.models import User
from django.test import TestCase

from brain.models import Folder, Patient, VoiceCaption
from common.models import Project, ProjectAccess
from common.permissions import (
    filter_patients_for_user,
    user_can_read_folder,
    user_can_view_caption_content,
    user_can_write_annotations,
)


class BrainProjectAclTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Brain ACL", slug="brain-acl", domain="brain"
        )
        self.admin = User.objects.create_user(username="admin_b", password="x")
        self.viewer = User.objects.create_user(username="viewer_b", password="x")
        self.annotator = User.objects.create_user(username="annotator_b", password="x")
        self.other = User.objects.create_user(username="other_b", password="x")

        ProjectAccess.objects.create(user=self.admin, project=self.project, role="admin")
        ProjectAccess.objects.create(user=self.viewer, project=self.project, role="viewer")
        ProjectAccess.objects.create(user=self.annotator, project=self.project, role="annotator")

        self.folder = Folder.objects.create(name="B1", project=self.project)
        self.patient = Patient.objects.create(
            name="PB", folder=self.folder, project=self.project
        )

    def test_brain_non_admin_without_project_access_sees_nothing(self):
        qs = filter_patients_for_user(self.other, Patient.objects.all(), "brain")
        self.assertEqual(qs.count(), 0)

    def test_brain_project_member_sees_patients(self):
        qs = filter_patients_for_user(self.viewer, Patient.objects.all(), "brain")
        self.assertEqual(qs.count(), 1)
        self.assertTrue(user_can_read_folder(self.viewer, self.folder, self.project))
        self.assertFalse(user_can_write_annotations(self.viewer, self.folder, self.project))

    def test_brain_annotator_can_write(self):
        self.assertTrue(user_can_write_annotations(self.annotator, self.folder, self.project))

    def test_brain_admin_bypass(self):
        qs = filter_patients_for_user(self.admin, Patient.objects.all(), "brain")
        self.assertEqual(qs.count(), 1)

    def test_brain_caption_content_visibility_by_project_role(self):
        owner = User.objects.create_user(username="brain_caption_owner", password="x")
        viewer = User.objects.create_user(username="brain_caption_viewer", password="x")
        annotator = User.objects.create_user(username="brain_caption_annotator", password="x")
        pm = User.objects.create_user(username="brain_caption_admin", password="x")

        ProjectAccess.objects.create(user=owner, project=self.project, role="viewer")
        ProjectAccess.objects.create(user=viewer, project=self.project, role="viewer")
        ProjectAccess.objects.create(user=annotator, project=self.project, role="annotator")
        ProjectAccess.objects.create(user=pm, project=self.project, role="admin")

        caption = VoiceCaption.objects.create(patient=self.patient, user=owner, duration=1.0)

        self.assertTrue(user_can_view_caption_content(owner, caption, self.project))
        self.assertTrue(user_can_view_caption_content(self.admin, caption, self.project))
        self.assertTrue(user_can_view_caption_content(viewer, caption, self.project))
        self.assertTrue(user_can_view_caption_content(pm, caption, self.project))
        self.assertFalse(user_can_view_caption_content(annotator, caption, self.project))
