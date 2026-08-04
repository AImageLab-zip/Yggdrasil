import contextlib
import hashlib
import json
import tempfile
from pathlib import Path
from unittest import mock

import nibabel as nib
import numpy as np
from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from common.models import FileRegistry, Job, Modality, Project, ProjectAccess
from maxillo.models import PanoramicState, Patient
from maxillo.views.metadata import _active_cbct_path
from maxillo.views.patient_detail import _resolved_cbct_viewer_source


@override_settings(SECURE_SSL_REDIRECT=False)
class NiftiMetadataTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.update_or_create(
            slug="maxillo", defaults={"name": "maxillo"}
        )
        self.user = User.objects.create_user(username="metadata-admin", password="x")
        ProjectAccess.objects.create(
            user=self.user, project=self.project, role="admin"
        )
        self.client.force_login(self.user)
        self.patient = Patient.objects.create(name="Metadata patient")
        self.modality = Modality.objects.create(name="Metadata CBCT", slug="cbct")
        self.patient.modalities.add(self.modality)
        self.affine = np.eye(4)

    def _file(self, file_type, path, *, job=None, subtype="", metadata=None):
        return FileRegistry.objects.create(
            file_type=file_type,
            file_path=path,
            file_size=1,
            file_hash="multi-file" if metadata else path,
            patient=self.patient,
            modality=self.modality,
            processing_job=job,
            subtype=subtype,
            metadata=metadata or {},
        )

    def _raw(self, path="maxillo/raw/metadata.nii.gz"):
        return self._file("cbct_raw", path)

    def _processed_bundle(self):
        volume_path = "maxillo/processed/display-volume.nii.gz"
        job = Job.objects.create(
            domain="maxillo",
            modality_slug="cbct",
            patient=self.patient,
            status="completed",
        )
        bundle = self._file(
            "cbct_processed",
            "maxillo/processed/bundle.json",
            job=job,
            metadata={
                "files": {
                    "volume_nifti": {"path": volume_path},
                    "segmentation_nifti": {
                        "path": "maxillo/processed/segmentation.nii.gz"
                    },
                }
            },
        )
        return {
            "job": job,
            "file": bundle,
            "volume_path": volume_path,
            "segmentation_path": "maxillo/processed/segmentation.nii.gz",
        }

    @contextlib.contextmanager
    def _download(self, key, suffix=".nii.gz"):
        with tempfile.TemporaryDirectory() as directory:
            local_path = str(Path(directory) / f"volume{suffix}")
            image = nib.Nifti1Image(
                np.zeros((2, 3, 4), dtype=np.int16), self.affine
            )
            nib.save(image, local_path)
            yield local_path

    @mock.patch("maxillo.views.patient_detail.artifact_exists", return_value=True)
    def test_processed_bundle_resolves_volume_output_not_segmentation(self, _exists):
        bundle = self._processed_bundle()

        self.assertEqual(_active_cbct_path(self.patient), bundle["volume_path"])

    def test_newest_valid_raw_nii_or_nii_gz_is_used_as_fallback(self):
        old_nii = self._raw("maxillo/raw/older.nii")
        self._raw("maxillo/raw/missing.nii.gz")
        newest_nii_gz = self._raw("maxillo/raw/newest.nii.gz")

        with mock.patch(
            "maxillo.views.patient_detail.artifact_exists",
            side_effect=lambda path: path != "maxillo/raw/missing.nii.gz",
        ):
            self.assertEqual(_active_cbct_path(self.patient), newest_nii_gz.file_path)

        newest_nii_gz.delete()
        with mock.patch(
            "maxillo.views.patient_detail.artifact_exists",
            side_effect=lambda path: path != "maxillo/raw/missing.nii.gz",
        ):
            self.assertEqual(_active_cbct_path(self.patient), old_nii.file_path)

    @mock.patch("maxillo.views.patient_detail.artifact_exists", return_value=True)
    @mock.patch("maxillo.views.metadata.download_to_tempfile")
    def test_metadata_read_is_get_only(self, download, _exists):
        self._raw()
        download.side_effect = self._download
        url = reverse(
            "maxillo:get_nifti_metadata",
            kwargs={"patient_id": self.patient.patient_id},
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["shape"], [2, 3, 4])
        self.assertEqual(self.client.post(url).status_code, 405)

    @mock.patch("maxillo.views.patient_detail.artifact_exists", return_value=True)
    @mock.patch("maxillo.views.metadata.get_object_storage")
    @mock.patch("maxillo.views.metadata.download_to_tempfile")
    def test_update_url_persists_affine_to_selected_object(
        self, download, get_storage, _exists
    ):
        self._raw("maxillo/raw/fallback.nii.gz")
        bundle = self._processed_bundle()
        download.side_effect = self._download
        storage = get_storage.return_value
        uploaded = {}

        def capture_upload(local_path, *, key, content_type):
            image = nib.load(local_path)
            with open(local_path, "rb") as uploaded_file:
                data = uploaded_file.read()
            uploaded[key] = {
                "affine": image.affine.copy(),
                "dtype": image.get_data_dtype(),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }

        storage.upload_file.side_effect = capture_upload
        source_before = _resolved_cbct_viewer_source(self.patient)
        PanoramicState.objects.create(
            patient=self.patient,
            source_job=bundle["job"],
            source_file=bundle["file"],
            source_file_key="volume_nifti",
            source_file_hash=source_before["file_hash"],
            source_segmentation_file=bundle["file"],
            source_segmentation_key="segmentation_nifti",
            source_segmentation_hash=source_before["segmentation_hash"],
            axial_slice=1,
            volume_shape=[2, 3, 4],
            spline=[[0, 0], [1, 0], [1, 1], [0, 1]],
            geometry_source="auto",
            default_mode="mip",
            request_hash="a" * 64,
            generated_by=self.user,
        )
        new_affine = [
            [1.0, 0.0, 0.0, 10.0],
            [0.0, 2.0, 0.0, 20.0],
            [0.0, 0.0, 3.0, 30.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
        url = reverse(
            "maxillo:update_nifti_metadata",
            kwargs={"patient_id": self.patient.patient_id},
        )

        response = self.client.post(
            url,
            data=json.dumps({"affine": new_affine}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(set(uploaded), {
            bundle["volume_path"],
            bundle["segmentation_path"],
        })
        for uploaded_artifact in uploaded.values():
            np.testing.assert_allclose(uploaded_artifact["affine"], new_affine)
            self.assertEqual(uploaded_artifact["dtype"], np.dtype("int16"))
        np.testing.assert_allclose(response.json()["affine"], new_affine)
        self.assertEqual(response.json()["shape"], [2, 3, 4])
        self.assertEqual(response.json()["data_type"], "int16")
        self.assertEqual(response.json()["orientation"], "RAS")
        self.assertEqual(storage.upload_file.call_count, 2)
        storage.upload_file.assert_has_calls(
            [
                mock.call(
                    mock.ANY,
                    key=bundle["segmentation_path"],
                    content_type="application/octet-stream",
                ),
                mock.call(
                    mock.ANY,
                    key=bundle["volume_path"],
                    content_type="application/octet-stream",
                ),
            ]
        )
        bundle["file"].refresh_from_db()
        nested = bundle["file"].metadata["files"]
        for key, path in (
            ("volume_nifti", bundle["volume_path"]),
            ("segmentation_nifti", bundle["segmentation_path"]),
        ):
            self.assertEqual(nested[key]["sha256"], uploaded[path]["sha256"])
            self.assertEqual(nested[key]["file_hash"], uploaded[path]["sha256"])
            self.assertEqual(nested[key]["file_size"], uploaded[path]["size"])
        source_after = _resolved_cbct_viewer_source(self.patient)
        self.assertNotEqual(source_after["file_hash"], source_before["file_hash"])
        self.assertNotEqual(
            source_after["segmentation_hash"], source_before["segmentation_hash"]
        )
        self.assertFalse(PanoramicState.objects.filter(patient=self.patient).exists())
        self.assertTrue(
            Job.objects.filter(
                patient=self.patient,
                modality_slug="metadata_update",
                status="completed",
            ).exists()
        )
        self.assertEqual(self.client.get(url).status_code, 405)

    @mock.patch("maxillo.views.patient_detail.artifact_exists", return_value=True)
    @mock.patch("maxillo.views.metadata.get_object_storage")
    @mock.patch("maxillo.views.metadata.download_to_tempfile")
    def test_partial_upload_failure_attempts_to_restore_original_pair(
        self, download, get_storage, _exists
    ):
        bundle = self._processed_bundle()
        download.side_effect = self._download
        calls = []

        def fail_volume_once(local_path, *, key, content_type):
            calls.append(key)
            if len(calls) == 2:
                raise RuntimeError("volume upload failed")

        get_storage.return_value.upload_file.side_effect = fail_volume_once
        response = self.client.post(
            reverse(
                "maxillo:update_nifti_metadata",
                kwargs={"patient_id": self.patient.patient_id},
            ),
            data=json.dumps({"affine": np.eye(4).tolist()}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            calls,
            [
                bundle["segmentation_path"],
                bundle["volume_path"],
                bundle["volume_path"],
                bundle["segmentation_path"],
            ],
        )
        bundle["file"].refresh_from_db()
        self.assertNotIn(
            "sha256", bundle["file"].metadata["files"]["volume_nifti"]
        )
        self.assertFalse(
            Job.objects.filter(
                patient=self.patient, modality_slug="metadata_update"
            ).exists()
        )

    @mock.patch("maxillo.views.patient_detail.artifact_exists", return_value=True)
    def test_rendered_page_exposes_sidebar_metadata_contract(self, _exists):
        self._raw()

        response = self.client.get(
            reverse(
                "maxillo:patient_detail",
                kwargs={"patient_id": self.patient.patient_id},
            )
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertContains(response, 'class="side-tab" data-tab-target="metadata"')
        self.assertContains(response, 'data-tab-pane="metadata"')
        self.assertContains(response, 'id="niftiMetadataContent"')
        self.assertContains(response, 'id="niftiMetadataDisplay" hidden')

    def test_frontend_uses_sidebar_activation_guard_and_update_endpoint(self):
        source = (Path(settings.BASE_DIR) / "static/js/nifti_metadata.js").read_text()

        self.assertIn('.side-tab[data-tab-target="metadata"]', source)
        self.assertIn("metadataLoadInFlight", source)
        self.assertIn("metadataLoaded", source)
        self.assertIn("/nifti-metadata/update/", source)
        self.assertIn("currentMetadata = data", source)
        self.assertIn("displayMetadata(data)", source)
        self.assertNotIn("currentMetadata.affine = newAffine", source)
        self.assertNotIn("shown.bs.collapse", source)
