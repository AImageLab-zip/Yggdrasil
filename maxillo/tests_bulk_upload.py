"""Bulk upload: one patient per file, administrators only, project-scoped."""
import tempfile

import nibabel as nib
import numpy as np
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from common.models import Modality, Project, ProjectAccess
from maxillo.models import Folder, Patient
from maxillo.views.patient_upload import modality_for_filename, patient_name_from_filename


def _nifti_upload(name):
    """A .nii.gz upload that satisfies the server-side orientation contract."""
    affine = np.eye(4)
    img = nib.Nifti1Image(np.zeros((2, 2, 2), dtype=np.int16), affine)
    img.set_qform(affine, code=1)
    img.set_sform(affine, code=1)
    with tempfile.NamedTemporaryFile(suffix=".nii.gz") as tmp:
        nib.save(img, tmp.name)
        tmp.seek(0)
        content = tmp.read()
    return SimpleUploadedFile(name, content, content_type="application/octet-stream")


class PatientNameFromFilenameTests(TestCase):
    def test_known_double_extensions_are_stripped_whole(self):
        self.assertEqual(patient_name_from_filename("tf4_case001.nii.gz"), "tf4_case001")
        self.assertEqual(patient_name_from_filename("archive.tar.gz"), "archive")

    def test_directory_components_are_dropped(self):
        self.assertEqual(patient_name_from_filename("batch/2026/case_7.mha"), "case_7")

    def test_unknown_extension_falls_back_to_splitext(self):
        self.assertEqual(patient_name_from_filename("case_7.foo"), "case_7")

    def test_name_is_capped_to_the_model_field_length(self):
        self.assertEqual(len(patient_name_from_filename("x" * 250 + ".nii.gz")), 100)

    def test_extensionless_name_survives(self):
        self.assertEqual(patient_name_from_filename("case_7"), "case_7")


class ModalityForFilenameTests(TestCase):
    def setUp(self):
        self.cbct, _ = Modality.objects.get_or_create(
            slug="cbct", defaults={"name": "CBCT", "supported_extensions": [".nii.gz", ".nii", ".mha"]}
        )
        self.cbct.supported_extensions = [".nii.gz", ".nii", ".mha"]
        self.cbct.save(update_fields=["supported_extensions"])
        self.panoramic, _ = Modality.objects.get_or_create(
            slug="panoramic", defaults={"name": "Panoramic", "supported_extensions": [".png", ".jpg"]}
        )
        self.panoramic.supported_extensions = [".png", ".jpg"]
        self.panoramic.save(update_fields=["supported_extensions"])
        self.telerad, _ = Modality.objects.get_or_create(
            slug="teleradiography", defaults={"name": "Teleradiography", "supported_extensions": [".png", ".jpg"]}
        )
        self.telerad.supported_extensions = [".png", ".jpg"]
        self.telerad.save(update_fields=["supported_extensions"])

    def test_extension_unique_to_one_project_modality_resolves(self):
        modality, error = modality_for_filename("case.nii.gz", [self.cbct, self.panoramic])
        self.assertEqual(modality, self.cbct)
        self.assertIsNone(error)

    def test_extension_shared_by_several_modalities_is_reported_not_guessed(self):
        modality, error = modality_for_filename("photo.png", [self.panoramic, self.telerad])
        self.assertIsNone(modality)
        self.assertIn("shared by several", error)

    def test_extension_absent_from_the_project_is_rejected(self):
        modality, error = modality_for_filename("mesh.stl", [self.cbct, self.panoramic])
        self.assertIsNone(modality)
        self.assertIn("unsupported file type", error)

    def test_a_png_project_with_only_one_image_modality_resolves(self):
        modality, error = modality_for_filename("photo.png", [self.cbct, self.panoramic])
        self.assertEqual(modality, self.panoramic)
        self.assertIsNone(error)


class BulkUploadViewTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="TF4 Test Set", slug="tf4-bulk", domain="maxillo")
        self.cbct, _ = Modality.objects.get_or_create(slug="cbct", defaults={"name": "CBCT"})
        self.cbct.supported_extensions = [".nii.gz", ".nii", ".mha"]
        self.cbct.is_active = True
        self.cbct.save(update_fields=["supported_extensions", "is_active"])
        self.project.modalities.set([self.cbct])
        self.folder = Folder.objects.create(name="Batch", project=self.project)

        self.admin = User.objects.create_user(username="bulk-admin", password="x")
        ProjectAccess.objects.create(user=self.admin, project=self.project, role="admin")
        self.annotator = User.objects.create_user(username="bulk-annotator", password="x")
        ProjectAccess.objects.create(user=self.annotator, project=self.project, role="annotator")

    def _login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

    def test_annotators_cannot_reach_bulk_upload(self):
        self._login(self.annotator)
        response = self.client.get(reverse("maxillo:bulk_upload_patients"))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Patient.objects.filter(project=self.project).exists())

    def test_admin_sees_the_form_without_a_scan_name_field(self):
        self._login(self.admin)
        response = self.client.get(reverse("maxillo:bulk_upload_patients"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bulk upload")
        self.assertNotContains(response, "Scan Name")
        # Only the project's own modalities are offered.
        self.assertContains(response, "CBCT")

    def test_each_file_becomes_one_patient_named_after_it(self):
        self._login(self.admin)
        response = self.client.post(
            reverse("maxillo:bulk_upload_patients"),
            {
                "folder": str(self.folder.id),
                "files": [_nifti_upload("tf4_case001.nii.gz"), _nifti_upload("tf4_case002.nii.gz")],
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["created"], 2)
        self.assertEqual(payload["failed"], 0)
        names = sorted(Patient.objects.filter(project=self.project).values_list("name", flat=True))
        self.assertEqual(names, ["tf4_case001", "tf4_case002"])
        for patient in Patient.objects.filter(project=self.project):
            self.assertEqual(patient.folder, self.folder)
            self.assertEqual(patient.uploaded_by, self.admin)
            self.assertEqual([m.slug for m in patient.modalities.all()], ["cbct"])

    def test_a_rejected_file_leaves_no_patient_and_does_not_abort_the_batch(self):
        self._login(self.admin)
        response = self.client.post(
            reverse("maxillo:bulk_upload_patients"),
            {
                "folder": str(self.folder.id),
                # A .nii.gz without orientation metadata fails the server contract.
                "files": [
                    _nifti_upload("good_case.nii.gz"),
                    SimpleUploadedFile("broken_case.nii.gz", b"not a nifti"),
                ],
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        payload = response.json()
        self.assertEqual(payload["created"], 1)
        self.assertEqual(payload["failed"], 1)
        self.assertEqual(
            sorted(Patient.objects.filter(project=self.project).values_list("name", flat=True)),
            ["good_case"],
        )
        failure = next(item for item in payload["results"] if not item["ok"])
        self.assertEqual(failure["file"], "broken_case.nii.gz")
        self.assertTrue(failure["error"])

    def test_a_folder_from_another_project_is_refused(self):
        other_project = Project.objects.create(name="Other", slug="other-bulk", domain="maxillo")
        other_folder = Folder.objects.create(name="Elsewhere", project=other_project)
        self._login(self.admin)

        response = self.client.post(
            reverse("maxillo:bulk_upload_patients"),
            {"folder": str(other_folder.id), "files": [_nifti_upload("case.nii.gz")]},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("folder of this project", response.json()["error"])
        self.assertFalse(Patient.objects.exists())

    def test_a_modality_the_project_does_not_enable_is_refused(self):
        self._login(self.admin)
        Modality.objects.get_or_create(slug="ios", defaults={"name": "IOS"})

        response = self.client.post(
            reverse("maxillo:bulk_upload_patients"),
            {
                "folder": str(self.folder.id),
                "modality": "ios",
                "files": [_nifti_upload("case.nii.gz")],
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("not enabled for this project", response.json()["error"])
        self.assertFalse(Patient.objects.exists())

    def test_a_file_type_outside_the_project_is_reported_per_file(self):
        self._login(self.admin)
        response = self.client.post(
            reverse("maxillo:bulk_upload_patients"),
            {"folder": str(self.folder.id), "files": [SimpleUploadedFile("mesh.stl", b"solid")]},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        payload = response.json()
        self.assertEqual(payload["created"], 0)
        self.assertIn("unsupported file type", payload["results"][0]["error"])
        self.assertFalse(Patient.objects.exists())

    def test_bulk_upload_link_is_admin_and_project_scoped(self):
        self._login(self.admin)
        admin_response = self.client.get(reverse("maxillo:patient_list"))
        self.assertEqual(
            admin_response.context["bulk_upload_url"], reverse("maxillo:bulk_upload_patients")
        )

        self.client.force_login(self.annotator)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()
        annotator_response = self.client.get(reverse("maxillo:patient_list"))
        self.assertIsNone(annotator_response.context["bulk_upload_url"])

    def test_upload_page_offers_bulk_mode_to_admins_only(self):
        self._login(self.admin)
        response = self.client.get(reverse("maxillo:upload_patient"))
        self.assertEqual(
            response.context["bulk_upload_url"], reverse("maxillo:bulk_upload_patients")
        )
        self.assertContains(response, "upload-modes")

        self.client.force_login(self.annotator)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()
        annotator_response = self.client.get(reverse("maxillo:upload_patient"))
        self.assertIsNone(annotator_response.context["bulk_upload_url"])
        self.assertNotContains(annotator_response, "upload-modes")

    def test_upload_page_bulk_mode_stays_in_its_namespace(self):
        # The same upload view is mounted per domain, so the switch must reverse
        # the bulk URL of the namespace the request came through.
        lap_project = Project.objects.create(
            name="Lap Set", slug="lap-bulk", domain="laparoscopy"
        )
        ProjectAccess.objects.create(user=self.admin, project=lap_project, role="admin")
        self.client.force_login(self.admin)
        session = self.client.session
        session["current_project_id"] = lap_project.id
        session.save()

        response = self.client.get(reverse("laparoscopy:upload_patient"))
        self.assertEqual(
            response.context["bulk_upload_url"],
            reverse("laparoscopy:bulk_upload_patients"),
        )


class PanoramicWarmupTests(TestCase):
    """Batch generation of default panoramics for already-uploaded patients."""

    def setUp(self):
        from common.models import FileRegistry, Job
        from maxillo.models import PanoramicState

        self.FileRegistry = FileRegistry
        self.Job = Job
        self.PanoramicState = PanoramicState

        self.project = Project.objects.create(
            name="Warmup", slug="warmup-project", domain="maxillo"
        )
        cbct, _ = Modality.objects.get_or_create(slug="cbct", defaults={"name": "CBCT"})
        self.project.modalities.set([cbct])
        self.folder = Folder.objects.create(name="Batch", project=self.project)
        self.cbct = cbct

        self.admin = User.objects.create_user(username="warmup-admin", password="x")
        ProjectAccess.objects.create(user=self.admin, project=self.project, role="admin")
        self.annotator = User.objects.create_user(username="warmup-annotator", password="x")
        ProjectAccess.objects.create(user=self.annotator, project=self.project, role="annotator")

    def _login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

    def _processed_patient(self, name):
        patient = Patient.objects.create(name=name, project=self.project, folder=self.folder)
        job = self.Job.objects.create(
            domain="maxillo", patient=patient, modality_slug="cbct", status="completed"
        )
        self.FileRegistry.objects.create(
            patient=patient,
            domain="maxillo",
            file_type="cbct_processed",
            file_path=f"maxillo/processed/cbct/{name}.nii.gz",
            file_size=8,
            file_hash=name.ljust(64, "0")[:64],
            modality=self.cbct,
            processing_job=job,
        )
        return patient

    def test_only_admins_can_reach_the_warmup_page(self):
        self._login(self.annotator)
        self.assertEqual(self.client.get(reverse("maxillo:panoramic_warmup")).status_code, 302)
        self._login(self.admin)
        self.assertEqual(self.client.get(reverse("maxillo:panoramic_warmup")).status_code, 200)

    def test_pending_lists_processed_patients_without_a_panoramic(self):
        pending = self._processed_patient("needs_one")
        # A patient whose CBCT has not been processed cannot be reconstructed yet.
        Patient.objects.create(name="unprocessed", project=self.project, folder=self.folder)

        self._login(self.admin)
        response = self.client.get(
            reverse("maxillo:panoramic_warmup_pending"), {"folder": self.folder.id}
        )

        payload = response.json()
        self.assertEqual([row["id"] for row in payload["patients"]], [pending.patient_id])
        self.assertEqual(payload["total"], 1)
        self.assertFalse(payload["truncated"])

    def test_a_patient_with_a_current_panoramic_is_not_listed(self):
        patient = self._processed_patient("already_done")
        self.PanoramicState.objects.create(
            patient=patient,
            source_file_key="volume_nifti",
            source_file_hash="d" * 64,
            axial_slice=10,
            volume_shape=[2, 2, 2],
            spline=[[0, 0], [1, 1], [2, 2], [3, 3]],
            default_mode="mip",
            algorithm_version="panorex-js-v2-mip",
            request_hash="e" * 64,
        )

        self._login(self.admin)
        response = self.client.get(
            reverse("maxillo:panoramic_warmup_pending"), {"folder": self.folder.id}
        )

        self.assertEqual(response.json()["patients"], [])

    def test_a_stale_algorithm_panoramic_is_regenerated(self):
        patient = self._processed_patient("stale")
        self.PanoramicState.objects.create(
            patient=patient,
            source_file_key="volume_nifti",
            source_file_hash="f" * 64,
            axial_slice=10,
            volume_shape=[2, 2, 2],
            spline=[[0, 0], [1, 1], [2, 2], [3, 3]],
            default_mode="mip",
            algorithm_version="panorex-js-v1-stale",
            request_hash="0" * 64,
        )

        self._login(self.admin)
        response = self.client.get(
            reverse("maxillo:panoramic_warmup_pending"), {"folder": self.folder.id}
        )

        self.assertEqual([row["id"] for row in response.json()["patients"]], [patient.patient_id])

    def test_a_folder_outside_the_project_is_refused(self):
        other = Project.objects.create(name="Other WU", slug="other-wu", domain="maxillo")
        foreign = Folder.objects.create(name="Foreign", project=other)

        self._login(self.admin)
        response = self.client.get(
            reverse("maxillo:panoramic_warmup_pending"), {"folder": foreign.id}
        )

        self.assertEqual(response.status_code, 400)

    def test_annotators_cannot_list_pending_patients(self):
        self._login(self.annotator)
        response = self.client.get(
            reverse("maxillo:panoramic_warmup_pending"), {"folder": self.folder.id}
        )
        self.assertEqual(response.status_code, 403)
