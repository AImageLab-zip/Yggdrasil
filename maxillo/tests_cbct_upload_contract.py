import gzip
import io
import tempfile
import numpy as np
import nibabel as nib
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from common.models import Modality, Project
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

    def test_save_cbct_to_dataset_records_orientation_metadata(self):
        uploaded = _make_nifti_uploaded_file("input.nii.gz", qform_code=1, sform_code=1)
        key, job = save_cbct_to_dataset(self.patient, uploaded)
        self.assertTrue(key.endswith(".nii.gz"))
        reg = self.patient.files.get(file_type="cbct_raw")
        self.assertEqual(reg.metadata.get("orientation"), "RAS")
        self.assertEqual(reg.metadata.get("file_format"), "nifti_compressed")
        self.assertFalse(reg.metadata.get("needs_conversion"))

    def test_save_cbct_folder_raises_validation_error(self):
        with self.assertRaises(ValidationError) as ctx:
            save_cbct_folder_to_dataset(self.patient, [])
        self.assertIn("deprecated", str(ctx.exception))
