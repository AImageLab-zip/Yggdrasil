"""Exporting a *prefix* row: the folder upload that used to export as nothing.

Finding F13. Some ``FileRegistry`` rows have a ``file_path`` that is an
object-storage **prefix** rather than an object -- folder uploads, written by
``maxillo.file_utils.save_generic_modality_folder``, which record their members as a
**list** under ``metadata['files']``. ``artifact_exists(prefix)`` heads a key that does
not exist, raises, and the artifact was skipped with a warning and no error, so a
folder upload exported as nothing at all.

These tests were originally written against a natively-stored DICOM series, which was
the other producer of prefix rows. DICOM is gone; folder uploads are not, and they are
what this behaviour exists for now.
"""

import io
import zipfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from common.export_processing import ExportProcessor
from common.models import FileRegistry, Modality, Project
from maxillo.models import Folder, Patient


class RecordingStorage:
    """Records every object written, and can read it back the way the exporter does."""

    def __init__(self):
        self.objects = {}

    def upload_file(self, local_path, *, key, content_type=None, metadata=None):
        with open(local_path, "rb") as handle:
            self.objects[key] = handle.read()
        return mock.Mock(content_length=len(self.objects[key]))

    def exists(self, key):
        return key in self.objects

    def iter_bytes(self, key, chunk_size=1024 * 1024):
        yield self.objects[key]


class PrefixExportBase(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="pfx", slug="pfx", domain="maxillo")
        self.folder = Folder.objects.create(name="F", project=self.project)
        self.patient = Patient.objects.create(project=self.project, folder=self.folder)
        self.modality, _ = Modality.objects.get_or_create(
            slug="cbct", defaults={"name": "CBCT"}
        )
        self.storage = RecordingStorage()
        patcher = mock.patch(
            "common.uploads.get_object_storage", return_value=self.storage
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def upload_folder(self, count=3, modality_slug="photoset"):
        """A real generic folder upload, which is what produces a prefix row."""
        from maxillo.file_utils import save_generic_modality_folder

        files = [
            SimpleUploadedFile(f"slice_{index}.png", f"png-{index}".encode())
            for index in range(count)
        ]
        row, _job = save_generic_modality_folder(self.patient, modality_slug, files)
        return row


class PrefixRowExportTests(PrefixExportBase):
    """Finding F13: a prefix row exported as nothing at all."""

    def test_a_folder_upload_row_produces_one_entry_per_member(self):
        row = self.upload_folder(3)
        processor = ExportProcessor.__new__(ExportProcessor)

        artifact = mock.Mock(key="photoset.raw", nested_key=None, filename=None)
        artifact.resolve_output.return_value = {
            "path": row.file_path, "size": row.file_size
        }
        entry, size = processor._file_entry(self.patient, artifact, row)

        # Before this, `artifact_exists(prefix)` raised, the artifact was skipped with
        # a warning, and the uploaded folder was simply absent from the ZIP.
        self.assertIsNotNone(entry)
        self.assertEqual(entry["type"], "series")
        self.assertEqual(len(entry["members"]), 3)
        self.assertEqual(size, row.file_size)

    def test_a_single_object_row_is_still_an_ordinary_file_entry(self):
        row = FileRegistry.objects.create(
            file_type="cbct_raw", file_path="pfx/raw/cbct/v.nii.gz",
            file_size=4, file_hash="b" * 64, patient=self.patient, domain="maxillo",
        )
        self.storage.objects["pfx/raw/cbct/v.nii.gz"] = b"data"
        processor = ExportProcessor.__new__(ExportProcessor)
        artifact = mock.Mock(key="cbct.raw", nested_key=None, filename=None)
        artifact.resolve_output.return_value = {"path": row.file_path, "size": 4}

        with mock.patch("common.export_processing.artifact_exists", return_value=True):
            entry, _size = processor._file_entry(self.patient, artifact, row)
        self.assertEqual(entry["type"], "file")

    def test_a_processed_bundle_is_not_mistaken_for_a_prefix_row(self):
        """`metadata['files']` is a *dict* for a bundle and a *list* for a prefix row."""
        row = FileRegistry.objects.create(
            file_type="cbct_processed", file_path="pfx/processed/cbct/job_1",
            file_size=4, file_hash="c" * 64, patient=self.patient, domain="maxillo",
            metadata={"files": {"volume_nifti": {"path": "pfx/processed/cbct/v.nii.gz"}}},
        )
        self.assertIsNone(ExportProcessor._prefix_members(row))

    def test_every_member_lands_in_the_zip_under_its_own_directory(self):
        row = self.upload_folder(3)
        processor = ExportProcessor.__new__(ExportProcessor)
        artifact = mock.Mock(key="photoset.raw", nested_key=None, filename=None)
        artifact.zip_directory.return_value = "photoset/raw"
        artifact.resolve_output.return_value = {
            "path": row.file_path, "size": row.file_size
        }
        entry, _size = processor._file_entry(self.patient, artifact, row)

        buffer = io.BytesIO()
        with mock.patch("common.export_processing.artifact_exists", self.storage.exists), \
             mock.patch("common.export_processing.iter_artifact_bytes", self.storage.iter_bytes), \
             zipfile.ZipFile(buffer, "w") as zipf:
            used = set()
            written = processor._write_series(
                zipf, used, f"patient_{self.patient.pk}/photoset/raw",
                processor._entry_filename(entry), entry,
            )

        self.assertEqual(written, 3)
        with zipfile.ZipFile(buffer) as archive:
            names = archive.namelist()
        self.assertEqual(len(names), 3)
        for name in names:
            self.assertTrue(name.endswith(".png"), name)
            self.assertIn("photoset/raw", name)

    def test_the_panoramic_pngs_still_reach_the_export(self):
        """Decision #8, guarded where F13's fix could have broken it.

        The panoramic MIP and ray-sum are ordinary single-object rows, and the change
        that taught the exporter about prefix rows runs *before* the existence check
        every file entry used to take. A misclassification here would drop the two
        PNGs every existing export contains -- silently, since a skipped artifact is
        a warning. The roadmap asks for exactly this test by name (risk 15).
        """
        from common.export_catalog import artifact_by_key

        processor = ExportProcessor.__new__(ExportProcessor)
        for key, subtype, filename in (
            ("panoramic.mip", "mip", "panoramic_mip.png"),
            ("panoramic.raysum", "raysum", "panoramic_xray.png"),
        ):
            with self.subTest(artifact=key):
                path = f"pfx/derived/panoramic/{subtype}.png"
                row = FileRegistry.objects.create(
                    file_type="panoramic_processed", subtype=subtype, file_path=path,
                    file_size=9, file_hash=subtype * 8, patient=self.patient,
                    domain="maxillo",
                )
                self.storage.objects[path] = b"PNG-bytes"
                artifact = artifact_by_key("maxillo", key)

                with mock.patch(
                    "common.export_processing.artifact_exists", self.storage.exists
                ):
                    entry, _size = processor._file_entry(self.patient, artifact, row)

                self.assertIsNotNone(entry, f"{key} was dropped from the export")
                self.assertEqual(entry["type"], "file")
                self.assertEqual(processor._entry_filename(entry), filename)
                self.assertEqual(
                    f"{processor._patient_folder(self.patient)}/{artifact.zip_directory()}"
                    f"/{filename}".split("/", 1)[1],
                    f"panoramic/generated/{filename}",
                )
