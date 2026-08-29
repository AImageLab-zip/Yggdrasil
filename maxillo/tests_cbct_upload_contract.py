import tempfile
from unittest import mock
import numpy as np
import nibabel as nib
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from common.dicom.models import DicomSeries
from common.models import Modality, Project
from common.tests_dicom import synthetic_instance
from common.tests_dicom_ingest import FakeStorage, as_upload, series_of
from maxillo.models import Folder, Patient
from maxillo.file_utils import (
    _validate_and_extract_nifti_orientation,
    save_cbct_to_dataset,
    save_cbct_folder_to_dataset,
)


def _make_nifti_uploaded_file(name="scan.nii.gz", qform_code=1, sform_code=1, affine=None):
    if affine is None:
        affine = np.eye(4)
    img = nib.Nifti1Image(np.zeros((3, 3, 3), dtype=np.int16), affine)
    img.set_qform(affine, code=qform_code)
    img.set_sform(affine, code=sform_code)

    with tempfile.NamedTemporaryFile(suffix=".nii.gz") as tmp:
        nib.save(img, tmp.name)
        tmp.seek(0)
        content = tmp.read()

    return SimpleUploadedFile(name, content, content_type="application/octet-stream")


class CBCTUploadContractTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.get_or_create(slug="maxillo", name="Maxillo", domain="maxillo")
        self.modality, _ = Modality.objects.get_or_create(slug="cbct", name="CBCT")
        self.folder = Folder.objects.create(name="General", project=self.project)
        self.patient = Patient.objects.create(
            name="CBCT Contract Patient", project=self.project, folder=self.folder
        )

    def test_valid_nii_gz_with_metadata_passes_validation(self):
        uploaded = _make_nifti_uploaded_file("volume.nii.gz", qform_code=1, sform_code=1)
        orientation = _validate_and_extract_nifti_orientation(uploaded)
        self.assertEqual(orientation, "RAS")

    def test_codes_zero_raises_validation_error(self):
        uploaded = _make_nifti_uploaded_file("volume.nii.gz", qform_code=0, sform_code=0)
        with self.assertRaises(ValidationError) as ctx:
            _validate_and_extract_nifti_orientation(uploaded)
        self.assertIn("codes are 0", str(ctx.exception))

    def test_non_nii_gz_file_raises_validation_error(self):
        uploaded = SimpleUploadedFile("volume.dcm", b"DICM data", content_type="application/dicom")
        with self.assertRaises(ValidationError) as ctx:
            _validate_and_extract_nifti_orientation(uploaded)
        self.assertIn("requires a compressed NIfTI file (.nii.gz)", str(ctx.exception))
        # The message no longer tells the user to convert DICOM first: since Phase 8
        # a DICOM upload is stored as DICOM, and this validator is simply not the
        # path it takes.
        self.assertNotIn("convert DICOM", str(ctx.exception))

    def test_save_cbct_to_dataset_records_orientation_metadata(self):
        uploaded = _make_nifti_uploaded_file("input.nii.gz", qform_code=1, sform_code=1)
        key, job = save_cbct_to_dataset(self.patient, uploaded)
        self.assertTrue(key.endswith(".nii.gz"))
        reg = self.patient.files.get(file_type="cbct_raw")
        self.assertEqual(reg.metadata.get("orientation"), "RAS")
        self.assertEqual(reg.metadata.get("file_format"), "nifti_compressed")
        self.assertFalse(reg.metadata.get("needs_conversion"))


@override_settings(DICOM_UID_HMAC_KEY="cbct-contract-key")
class CBCTDicomUploadTests(TestCase):
    """A DICOM CBCT is stored as DICOM, through the same two upload controls.

    The behaviour this replaces: ``save_cbct_folder_to_dataset`` raised
    unconditionally, because a DICOM folder was converted to a ``.nii.gz`` in the
    browser and the series was discarded before the server ever saw it.
    """

    def setUp(self):
        self.project, _ = Project.objects.get_or_create(
            slug="maxillo", name="Maxillo", domain="maxillo"
        )
        self.modality, _ = Modality.objects.get_or_create(slug="cbct", name="CBCT")
        self.folder = Folder.objects.create(name="General", project=self.project)
        self.patient = Patient.objects.create(
            name="DICOM CBCT Patient", project=self.project, folder=self.folder
        )
        self.storage = FakeStorage()
        patcher = mock.patch(
            "common.dicom.ingest.get_object_storage", return_value=self.storage
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_dicom_folder_is_stored_as_dicom(self):
        prefix, _job = save_cbct_folder_to_dataset(self.patient, series_of(3))

        row = self.patient.files.get(file_type="cbct_raw")
        self.assertEqual(row.file_path, prefix)
        self.assertEqual(row.metadata["file_format"], "dicom")
        self.assertEqual(row.metadata["file_count"], 3)
        self.assertEqual(row.modality, self.modality)
        self.assertEqual(DicomSeries.objects.get().instance_count, 3)
        # Not a NIfTI anywhere: that is the whole point of the phase.
        self.assertFalse(any(k.endswith(".nii.gz") for k in self.storage.objects))

    def test_a_single_dicom_file_takes_the_same_path(self):
        # The File control has advertised '.dcm' since it existed; until now that
        # promise was kept by a browser conversion that threw the DICOM away.
        prefix, _job = save_cbct_to_dataset(self.patient, series_of(1)[0])
        self.assertEqual(self.patient.files.get(file_type="cbct_raw").file_path, prefix)
        self.assertEqual(DicomSeries.objects.get().instance_count, 1)

    def test_the_job_input_names_the_series_not_a_converted_file(self):
        prefix, job = save_cbct_folder_to_dataset(self.patient, series_of(2))
        if job is not None:  # only when a CBCT runner route is configured
            self.assertEqual(job.input_files, {"input": prefix})

    def test_a_folder_holding_two_series_stores_both_and_reports_the_larger(self):
        # The deleted browser converter threw every slice in the folder into one
        # volume regardless of SeriesInstanceUID, so a scout beside the volume came
        # out interleaved. Series are separated now.
        files = series_of(2, series_uid="1.2.3.900") + series_of(5, series_uid="1.2.3.901")
        prefix, _job = save_cbct_folder_to_dataset(self.patient, files)
        self.assertEqual(DicomSeries.objects.count(), 2)
        self.assertEqual(DicomSeries.objects.get(file__file_path=prefix).instance_count, 5)

    def test_a_refused_upload_reports_every_reason_and_stores_nothing(self):
        files = series_of(1) + [
            as_upload(synthetic_instance(sop_instance_uid="1.2.3.90", burned_in="YES")),
            as_upload(synthetic_instance(
                sop_instance_uid="1.2.3.91", sop_class_uid="1.2.840.10008.5.1.4.1.1.7"
            )),
        ]
        with self.assertRaises(ValidationError) as ctx:
            save_cbct_folder_to_dataset(self.patient, files)
        self.assertEqual(len(ctx.exception.messages), 2)
        self.assertEqual(self.storage.objects, {})
        self.assertEqual(self.patient.files.count(), 0)
