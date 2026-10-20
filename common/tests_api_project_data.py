"""/api/<slug>/folders|patients|upload/: the same API for every domain.

Storage is never touched: each test patches the domain's save function, which is
where the bytes would leave. What is checked is everything around it -- which
domain's tables the patient lands in, who may write where, and that a field the
project does not accept is refused rather than dropped.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from common import domain_uploads
from common.api_tokens import issue_token
from common.domains import DOMAINS
from common.models import Modality, Project, ProjectAccess

BrainPatient = apps.get_model("brain", "Patient")
BrainFolder = apps.get_model("brain", "Folder")
MaxilloPatient = apps.get_model("maxillo", "Patient")
MaxilloFolder = apps.get_model("maxillo", "Folder")


def nifti(name="volume.nii.gz"):
    return SimpleUploadedFile(name, b"not really a volume", content_type="application/gzip")


class ProjectDataApiTestBase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin1", password="pw")
        self.annotator = User.objects.create_user("annotator1", password="pw")
        self.viewer = User.objects.create_user("viewer1", password="pw")
        self.outsider = User.objects.create_user("outsider1", password="pw")

        self.t1, _ = Modality.objects.get_or_create(
            slug="braintumor-mri-t1", defaults={"name": "Brain MRI T1", "domain": "brain"}
        )
        self.flair, _ = Modality.objects.get_or_create(
            slug="braintumor-mri-flair", defaults={"name": "Brain MRI FLAIR", "domain": "brain"}
        )
        self.brain = Project.objects.create(name="BATCH_01", slug="batch-01", domain="brain")
        self.brain.modalities.add(self.t1)  # FLAIR deliberately not enabled
        for user, role in ((self.admin, "admin"), (self.annotator, "annotator"), (self.viewer, "viewer")):
            ProjectAccess.objects.create(user=user, project=self.brain, role=role)
        self.brain_folder = BrainFolder.objects.create(name="BATCH_01", project=self.brain)

    def as_user(self, user):
        _token, key = issue_token(user, "test")
        return {"HTTP_AUTHORIZATION": f"Bearer {key}"}

    def upload(self, user, slug, **data):
        return self.client.post(f"/api/{slug}/upload/", data, **self.as_user(user))


class RegistryTests(TestCase):
    def test_every_domain_has_an_upload_handler(self):
        self.assertEqual(domain_uploads.registered_domains(), DOMAINS)


class FolderApiTests(ProjectDataApiTestBase):
    def create_folder(self, user, name):
        return self.client.post(
            "/api/batch-01/folders/", json.dumps({"name": name}),
            content_type="application/json", **self.as_user(user),
        )

    def test_get_or_create(self):
        first = self.create_folder(self.admin, "BATCH_02")
        self.assertEqual(first.status_code, 201, first.content)
        again = self.create_folder(self.admin, "BATCH_02")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(first.json()["folder"]["id"], again.json()["folder"]["id"])
        # In brain's table, inside this project.
        folder = BrainFolder.objects.get(id=first.json()["folder"]["id"])
        self.assertEqual(folder.project, self.brain)
        self.assertFalse(MaxilloFolder.objects.filter(name="BATCH_02").exists())

    def test_only_project_admins_create(self):
        self.assertEqual(self.create_folder(self.annotator, "Nope").status_code, 403)
        self.assertEqual(self.create_folder(self.outsider, "Nope").status_code, 404)
        self.assertFalse(BrainFolder.objects.filter(name="Nope").exists())

    def test_the_list_is_this_domains_folders(self):
        response = self.client.get("/api/batch-01/folders/", **self.as_user(self.viewer))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([f["name"] for f in response.json()["folders"]], ["BATCH_01"])


@patch("brain.upload_api.save_brain_modality_file")
class BrainUploadTests(ProjectDataApiTestBase):
    def test_an_annotator_uploads_into_brains_tables(self, save):
        save.return_value = (object(), SimpleNamespace(id=7, status="completed"))
        response = self.upload(
            self.annotator, "batch-01",
            name="BraTS-GLI-00002-000", folder=self.brain_folder.id,
            **{"braintumor-mri-t1": nifti()},
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()["patient"]
        patient = BrainPatient.objects.get(patient_id=body["patient_id"])
        self.assertEqual(patient.folder, self.brain_folder)
        self.assertEqual(patient.uploaded_by, self.annotator)
        self.assertEqual([m["slug"] for m in body["modalities"]], ["braintumor-mri-t1"])
        self.assertEqual(body["upload_results"]["jobs"], [{"id": 7, "type": "braintumor-mri-t1", "status": "completed"}])
        self.assertFalse(MaxilloPatient.all_objects.filter(name="BraTS-GLI-00002-000").exists())
        save.assert_called_once()

    def test_a_modality_the_project_does_not_enable_is_refused(self, save):
        response = self.upload(
            self.annotator, "batch-01", name="X", folder=self.brain_folder.id,
            **{"braintumor-mri-t1": nifti(), "braintumor-mri-flair": nifti()},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["rejected"], ["braintumor-mri-flair"])
        self.assertEqual(response.json()["accepted"], ["braintumor-mri-t1"])
        self.assertFalse(BrainPatient.all_objects.exists())
        save.assert_not_called()

    def test_no_file_is_refused(self, save):
        response = self.upload(self.annotator, "batch-01", name="X", folder=self.brain_folder.id)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(BrainPatient.all_objects.exists())

    def test_the_folder_must_belong_to_the_project(self, save):
        other = Project.objects.create(name="Other", slug="other", domain="brain")
        foreign = BrainFolder.objects.create(name="Elsewhere", project=other)
        response = self.upload(
            self.annotator, "batch-01", name="X", folder=foreign.id,
            **{"braintumor-mri-t1": nifti()},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(BrainPatient.all_objects.exists())

    def test_viewers_and_outsiders_cannot_upload(self, save):
        for user, status in ((self.viewer, 403), (self.outsider, 404)):
            with self.subTest(user=user.username):
                response = self.upload(
                    user, "batch-01", name="X", folder=self.brain_folder.id,
                    **{"braintumor-mri-t1": nifti()},
                )
                self.assertEqual(response.status_code, status)
        self.assertFalse(BrainPatient.all_objects.exists())

    def test_a_failing_save_rolls_the_patient_back(self, save):
        save.side_effect = RuntimeError("storage down")
        with self.assertLogs("common.project_data_api", level="ERROR"):
            response = self.upload(
                self.annotator, "batch-01", name="X", folder=self.brain_folder.id,
                **{"braintumor-mri-t1": nifti()},
            )
        self.assertEqual(response.status_code, 500)
        self.assertFalse(BrainPatient.all_objects.exists())

    def test_the_project_detail_lists_the_accepted_fields(self, save):
        response = self.client.get("/api/projects/batch-01/", **self.as_user(self.viewer))
        self.assertEqual(
            [f["name"] for f in response.json()["upload_fields"]], ["braintumor-mri-t1"]
        )


@patch("brain.upload_api.save_brain_modality_file", return_value=(object(), None))
class PatientListTests(ProjectDataApiTestBase):
    def test_filter_by_folder_and_name(self, _save):
        second = BrainFolder.objects.create(name="BATCH_02", project=self.brain)
        for folder in (self.brain_folder, second):
            self.upload(
                self.annotator, "batch-01", name="BraTS-GLI-00002-000", folder=folder.id,
                **{"braintumor-mri-t1": nifti()},
            )
        auth = self.as_user(self.viewer)
        everything = self.client.get("/api/batch-01/patients/", **auth).json()
        self.assertEqual(everything["total_patients"], 2)
        in_second = self.client.get("/api/batch-01/patients/", {"folder": second.id}, **auth).json()
        self.assertEqual([p["folder"]["id"] for p in in_second["patients"]], [second.id])
        by_name = self.client.get("/api/batch-01/patients/", {"name": "nobody"}, **auth).json()
        self.assertEqual(by_name["patients"], [])


class MaxilloUploadTests(ProjectDataApiTestBase):
    def setUp(self):
        super().setUp()
        self.ios, _ = Modality.objects.get_or_create(slug="ios", defaults={"name": "IOS"})
        self.dental = Project.objects.create(name="Dental", slug="dental", domain="maxillo")
        self.dental.modalities.add(self.ios)
        ProjectAccess.objects.create(user=self.annotator, project=self.dental, role="annotator")
        self.dental_folder = MaxilloFolder.objects.create(name="Cases", project=self.dental)

    def stl(self, name):
        return SimpleUploadedFile(name, b"solid x", content_type="model/stl")

    @patch("maxillo.file_utils.save_ios_to_dataset")
    def test_old_ios_field_names_still_work(self, save_ios):
        save_ios.return_value = {"processing_job": SimpleNamespace(id=3, status="pending")}
        response = self.upload(
            self.annotator, "dental", name="P1", folder=self.dental_folder.id,
            upper_scan_raw=self.stl("u.stl"), lower_scan_raw=self.stl("l.stl"),
        )
        self.assertEqual(response.status_code, 200, response.content)
        patient = MaxilloPatient.objects.get(name="P1")
        self.assertEqual(list(patient.modalities.values_list("slug", flat=True)), ["ios"])
        save_ios.assert_called_once()

    @patch("maxillo.file_utils.save_ios_to_dataset")
    def test_one_ios_arch_alone_is_refused(self, save_ios):
        response = self.upload(
            self.annotator, "dental", name="P1", folder=self.dental_folder.id,
            ios_upper=self.stl("u.stl"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(MaxilloPatient.all_objects.exists())
        save_ios.assert_not_called()
