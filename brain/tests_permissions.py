from django.contrib.auth.models import User
from django.test import TestCase

from brain.models import Folder, FolderAccess, Patient
from common.models import Project, ProjectAccess
from common.permissions import filter_patients_for_user, user_can_read_folder, user_can_write_annotations


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
