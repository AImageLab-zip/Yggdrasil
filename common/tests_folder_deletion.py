"""Deleting a folder: same rule in all three domains, and it never takes patients.

The endpoint existed only for brain, so maxillo and laparoscopy could create
folders they had no way to remove -- and brain's copy counted only the folder's
*direct* patients, so a folder with populated sub-folders read as empty and took
them silently. One rule now lives in :mod:`common.deletion`; maxillo's view
serves laparoscopy too, because laparoscopy includes ``maxillo.app_urls`` under
its own namespace.
"""

import json

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from common.deletion import FolderNotEmpty, delete_folder
from common.models import Project, ProjectAccess


class FolderDeletionRuleTests(TestCase):
    def setUp(self):
        from maxillo.models import Folder, Patient

        self.Folder, self.Patient = Folder, Patient
        self.project = Project.objects.create(name="Rule", slug="rule", domain="maxillo")

    def test_an_empty_folder_goes(self):
        folder = self.Folder.objects.create(name="Empty", project=self.project)
        self.assertEqual(delete_folder(folder), 0)
        self.assertFalse(self.Folder.objects.filter(name="Empty").exists())

    def test_a_populated_folder_is_refused_with_its_count(self):
        folder = self.Folder.objects.create(name="Full", project=self.project)
        self.Patient.objects.create(project=self.project, folder=folder)

        with self.assertRaises(FolderNotEmpty) as caught:
            delete_folder(folder)
        self.assertEqual(caught.exception.patient_count, 1)
        self.assertTrue(self.Folder.objects.filter(id=folder.id).exists())

    def test_patients_in_sub_folders_count(self):
        """The old per-domain copy asked only about direct children."""
        parent = self.Folder.objects.create(name="Parent", project=self.project)
        child = self.Folder.objects.create(name="Child", project=self.project, parent=parent)
        self.Patient.objects.create(project=self.project, folder=child)

        with self.assertRaises(FolderNotEmpty):
            delete_folder(parent)

    def test_soft_deleted_patients_count(self):
        folder = self.Folder.objects.create(name="Soft", project=self.project)
        patient = self.Patient.objects.create(project=self.project, folder=folder)
        patient.deleted = True
        patient.save(update_fields=["deleted"])

        with self.assertRaises(FolderNotEmpty):
            delete_folder(folder)

    def test_forcing_unfiles_the_patients_and_keeps_them(self):
        parent = self.Folder.objects.create(name="Parent", project=self.project)
        child = self.Folder.objects.create(name="Child", project=self.project, parent=parent)
        patient = self.Patient.objects.create(project=self.project, folder=child)

        self.assertEqual(delete_folder(parent, force=True), 1)

        self.assertFalse(self.Folder.objects.filter(id__in=[parent.id, child.id]).exists())
        patient.refresh_from_db()
        # The invariant: a folder is a filing decision, not an owner.
        self.assertIsNone(patient.folder_id)
        self.assertEqual(patient.project_id, self.project.id)


class FolderDeleteEndpointTests(TestCase):
    """The same endpoint, reached through each domain's own namespace."""

    DOMAINS = ["maxillo", "brain", "laparoscopy"]

    def setUp(self):
        self.admin = User.objects.create_user("boss", password="x")
        self.outsider = User.objects.create_user("nobody", password="x")

    def _folder(self, domain):
        from django.apps import apps

        project = Project.objects.create(
            name=f"{domain} deletion", slug=f"{domain}-deletion", domain=domain
        )
        ProjectAccess.objects.create(user=self.admin, project=project, role="admin")
        ProjectAccess.objects.create(user=self.outsider, project=project, role="viewer")
        folder = apps.get_model(domain, "Folder").objects.create(name="Cases", project=project)
        return project, folder

    def _delete(self, domain, folder, *, force=False):
        url = reverse(f"{domain}:delete_folder", args=[folder.id])
        return self.client.delete(f"{url}?force=true" if force else url)

    def test_every_domain_can_delete_a_folder(self):
        for domain in self.DOMAINS:
            with self.subTest(domain=domain):
                project, folder = self._folder(domain)
                self.client.force_login(self.admin)
                self.client.session["current_project_id"] = project.id
                session = self.client.session
                session["current_project_id"] = project.id
                session.save()

                response = self._delete(domain, folder)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertTrue(json.loads(response.content)["success"])
                self.assertFalse(type(folder).objects.filter(id=folder.id).exists())

    def test_a_non_admin_may_not(self):
        for domain in self.DOMAINS:
            with self.subTest(domain=domain):
                project, folder = self._folder(domain)
                self.client.force_login(self.outsider)
                session = self.client.session
                session["current_project_id"] = project.id
                session.save()

                self.assertEqual(self._delete(domain, folder).status_code, 403)
                self.assertTrue(type(folder).objects.filter(id=folder.id).exists())

    def test_a_populated_folder_reports_the_count_and_yields_to_force(self):
        from django.apps import apps

        for domain in self.DOMAINS:
            with self.subTest(domain=domain):
                project, folder = self._folder(domain)
                Patient = apps.get_model(domain, "Patient")
                patient = Patient.objects.create(project=project, folder=folder)
                self.client.force_login(self.admin)
                session = self.client.session
                session["current_project_id"] = project.id
                session.save()

                refused = self._delete(domain, folder)
                self.assertEqual(refused.status_code, 400)
                self.assertEqual(json.loads(refused.content)["patient_count"], 1)

                forced = self._delete(domain, folder, force=True)
                self.assertEqual(forced.status_code, 200, forced.content)
                patient.refresh_from_db()
                self.assertIsNone(patient.folder_id)
