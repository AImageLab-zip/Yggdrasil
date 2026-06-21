from django.contrib.auth.models import User
from django.test import TestCase

from brain.models import Folder, FolderAccess, Patient, VoiceCaption
from common.models import Project, ProjectAccess
from common.permissions import (
    filter_patients_for_user,
    user_can_read_folder,
    user_can_view_caption_content,
    user_can_write_annotations,
)


class BrainFolderAclTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.get_or_create(name="brain", defaults={"slug": "brain"})
        self.admin = User.objects.create_user(username="admin_b", password="x")
        self.user = User.objects.create_user(username="user_b", password="x")

        ProjectAccess.objects.create(user=self.admin, project=self.project, role="admin")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="standard")

        self.folder = Folder.objects.create(name="B1")
        self.patient = Patient.objects.create(name="PB", folder=self.folder)

    def test_brain_non_admin_needs_folder_acl(self):
        qs = filter_patients_for_user(self.user, Patient.objects.all(), "brain")
        self.assertEqual(qs.count(), 0)

        FolderAccess.objects.create(user=self.user, folder=self.folder, role="standard")
        qs = filter_patients_for_user(self.user, Patient.objects.all(), "brain")
        self.assertEqual(qs.count(), 1)
        self.assertTrue(user_can_read_folder(self.user, self.folder, "brain"))
        self.assertFalse(user_can_write_annotations(self.user, self.folder, "brain"))

    def test_brain_admin_bypass(self):
        qs = filter_patients_for_user(self.admin, Patient.objects.all(), "brain")
        self.assertEqual(qs.count(), 1)

    def test_brain_caption_content_visibility_by_folder_role(self):
        owner = User.objects.create_user(username="brain_caption_owner", password="x")
        standard = User.objects.create_user(username="brain_caption_standard", password="x")
        annotator = User.objects.create_user(username="brain_caption_annotator", password="x")
        project_manager = User.objects.create_user(username="brain_caption_pm", password="x")

        ProjectAccess.objects.create(user=owner, project=self.project, role="standard")
        ProjectAccess.objects.create(user=standard, project=self.project, role="standard")
        ProjectAccess.objects.create(user=annotator, project=self.project, role="standard")
        ProjectAccess.objects.create(user=project_manager, project=self.project, role="standard")

        FolderAccess.objects.create(user=owner, folder=self.folder, role="annotator")
        FolderAccess.objects.create(user=standard, folder=self.folder, role="standard")
        FolderAccess.objects.create(user=annotator, folder=self.folder, role="annotator")
        FolderAccess.objects.create(user=project_manager, folder=self.folder, role="project_manager")

        caption = VoiceCaption.objects.create(patient=self.patient, user=owner, duration=1.0)

        self.assertTrue(user_can_view_caption_content(owner, caption, "brain"))
        self.assertTrue(user_can_view_caption_content(self.admin, caption, "brain"))
        self.assertTrue(user_can_view_caption_content(standard, caption, "brain"))
        self.assertTrue(user_can_view_caption_content(project_manager, caption, "brain"))
        self.assertFalse(user_can_view_caption_content(annotator, caption, "brain"))
