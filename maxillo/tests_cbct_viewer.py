from unittest import mock

from django.test import TestCase

from common.models import FileRegistry, Job, Modality, Project
from maxillo.models import Patient
from maxillo.views.patient_detail import _cbct_viewer_files


class CBCTViewerFilePairingTests(TestCase):
    def setUp(self):
        # Patients are project-scoped.
        self.project = Project.objects.create(
            name="CBCT Viewer", slug="cbct-viewer", domain="maxillo"
        )
        self.patient = Patient.objects.create(
            name="CBCT Viewer Patient", project=self.project
        )
        self.modality = Modality.objects.create(name="CBCT Viewer", slug="cbct")

    def _file(self, file_type, path, *, job=None, subtype="", metadata=None):
        return FileRegistry.objects.create(
            file_type=file_type,
            file_path=path,
            file_size=1,
            file_hash=path,
            patient=self.patient,
            modality=self.modality,
            processing_job=job,
            subtype=subtype,
            metadata=metadata or {},
        )

    @mock.patch("maxillo.views.patient_detail.artifact_exists", return_value=True)
    def test_pairs_direct_volume_and_segmentation_rows_from_same_job(self, _exists):
        job = Job.objects.create(
            domain="maxillo",
            modality_slug="cbct",
            patient=self.patient,
            status="completed",
            output_files={
                "volume_nifti": "processed/volume.nii.gz",
                "segmentation_nifti": {
                    "path": "processed/segmentation.nii.gz",
                    "label_max": 42,
                },
            },
        )
        volume = self._file(
            "cbct_processed",
            "processed/volume.nii.gz",
            job=job,
            subtype="volume_nifti",
        )
        segmentation = self._file(
            "cbct_processed",
            "processed/segmentation.nii.gz",
            job=job,
            subtype="segmentation_nifti",
        )

        display, file_key, segmentation_spec = _cbct_viewer_files(self.patient)

        self.assertEqual(display, volume)
        self.assertEqual(file_key, "primary")
        self.assertEqual(
            segmentation_spec,
            {"id": segmentation.id, "fileKey": "primary", "labelMax": 42},
        )

    @mock.patch("maxillo.views.patient_detail.artifact_exists", return_value=True)
    def test_pairs_raw_nifti_named_as_job_input_with_segmentation(self, _exists):
        raw = self._file("cbct_raw", "raw/input-volume.nii.gz")
        job = Job.objects.create(
            domain="maxillo",
            modality_slug="cbct",
            patient=self.patient,
            status="completed",
            input_files={"input": raw.file_path},
            output_files={"segmentation_nifti": "processed/segmentation.nii.gz"},
        )
        segmentation = self._file(
            "cbct_processed",
            "processed/segmentation.nii.gz",
            job=job,
            subtype="segmentation_nifti",
        )

        display, file_key, segmentation_spec = _cbct_viewer_files(self.patient)

        self.assertEqual(display, raw)
        self.assertEqual(file_key, "primary")
        self.assertEqual(segmentation_spec["id"], segmentation.id)
        self.assertEqual(segmentation_spec["fileKey"], "primary")
