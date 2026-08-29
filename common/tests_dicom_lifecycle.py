"""What happens to a stored series over its life: export, the lock, and discard_raw.

Three properties, each of which fails silently without a test. An export that omits
the raw scan looks like a smaller ZIP. A series nobody sealed accepts an instance
rewrite that re-bases every annotation. A ``discard_raw`` flag set on a DICOM modality
blanks the viewer while the bytes sit in storage.
"""

import io
import zipfile
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from annotations.constants import AnnotationOrigin
from annotations.models import SourceResource
from annotations.services import attach_target, get_or_create_set, record_revision
from common.dicom.ingest import DicomIngestError, ingest_dicom_series
from common.dicom.models import DicomSeries, SealedSeriesError
from common.export_processing import ExportProcessor
from common.models import Modality, ProcessingStep, Project, SystemCheck
from common.tasks import verify_dicom_deidentification
from common.tests_dicom_ingest import FakeStorage, series_of
from maxillo.models import Folder, Patient


class ReadingStorage(FakeStorage):
    def get(self, key):
        if key not in self.objects:
            raise FileNotFoundError(key)
        return io.BytesIO(self.objects[key]), mock.Mock()

    def exists(self, key):
        return key in self.objects

    def iter_bytes(self, key, chunk_size=1024 * 1024):
        yield self.objects[key]


@override_settings(DICOM_UID_HMAC_KEY="lifecycle-key")
class DicomLifecycleBase(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="life", slug="life", domain="maxillo")
        self.folder = Folder.objects.create(name="F", project=self.project)
        self.patient = Patient.objects.create(project=self.project, folder=self.folder)
        self.modality, _ = Modality.objects.get_or_create(slug="cbct", defaults={"name": "CBCT"})
        self.storage = ReadingStorage()
        patcher = mock.patch(
            "common.dicom.ingest.get_object_storage", return_value=self.storage
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def ingest(self, count=3):
        [series] = ingest_dicom_series(
            self.patient, modality_slug="cbct", file_type="cbct_raw",
            files=series_of(count), modality=self.modality,
        )
        return series


class SeriesExportTests(DicomLifecycleBase):
    """Finding F13: a prefix row exported as nothing at all."""

    def test_a_series_prefix_row_produces_one_entry_per_instance(self):
        series = self.ingest(3)
        processor = ExportProcessor.__new__(ExportProcessor)

        artifact = mock.Mock(key="cbct.raw", nested_key=None, filename=None)
        artifact.resolve_output.return_value = {
            "path": series.file.file_path, "size": series.file.file_size
        }
        entry, size = processor._file_entry(self.patient, artifact, series.file)

        # Before this, `artifact_exists(prefix)` raised, the artifact was skipped with
        # a warning, and the raw scan was simply absent from the ZIP.
        self.assertIsNotNone(entry)
        self.assertEqual(entry["type"], "series")
        self.assertEqual(len(entry["members"]), 3)
        self.assertEqual(size, series.file.file_size)

    def test_a_single_object_row_is_still_an_ordinary_file_entry(self):
        from common.models import FileRegistry

        row = FileRegistry.objects.create(
            file_type="cbct_raw", file_path="life/raw/cbct/v.nii.gz",
            file_size=4, file_hash="b" * 64, patient=self.patient, domain="maxillo",
        )
        self.storage.objects["life/raw/cbct/v.nii.gz"] = b"data"
        processor = ExportProcessor.__new__(ExportProcessor)
        artifact = mock.Mock(key="cbct.raw", nested_key=None, filename=None)
        artifact.resolve_output.return_value = {"path": row.file_path, "size": 4}

        with mock.patch("common.export_processing.artifact_exists", return_value=True):
            entry, _size = processor._file_entry(self.patient, artifact, row)
        self.assertEqual(entry["type"], "file")

    def test_a_processed_bundle_is_not_mistaken_for_a_series(self):
        """`metadata['files']` is a *dict* for a bundle and a *list* for a prefix row."""
        from common.models import FileRegistry

        row = FileRegistry.objects.create(
            file_type="cbct_processed", file_path="life/processed/cbct/job_1",
            file_size=4, file_hash="c" * 64, patient=self.patient, domain="maxillo",
            metadata={"files": {"volume_nifti": {"path": "life/processed/cbct/v.nii.gz"}}},
        )
        self.assertIsNone(ExportProcessor._prefix_members(row))

    def test_every_instance_lands_in_the_zip_under_its_own_directory(self):
        series = self.ingest(3)
        processor = ExportProcessor.__new__(ExportProcessor)
        artifact = mock.Mock(key="cbct.raw", nested_key=None, filename=None)
        artifact.zip_directory.return_value = "cbct/raw"
        artifact.resolve_output.return_value = {
            "path": series.file.file_path, "size": series.file.file_size
        }
        entry, _size = processor._file_entry(self.patient, artifact, series.file)

        buffer = io.BytesIO()
        with mock.patch("common.export_processing.artifact_exists", self.storage.exists), \
             mock.patch("common.export_processing.iter_artifact_bytes", self.storage.iter_bytes), \
             zipfile.ZipFile(buffer, "w") as zipf:
            used = set()
            written = processor._write_series(
                zipf, used, f"patient_{self.patient.pk}/cbct/raw",
                processor._entry_filename(entry), entry,
            )

        self.assertEqual(written, 3)
        with zipfile.ZipFile(buffer) as archive:
            names = archive.namelist()
        self.assertEqual(len(names), 3)
        for name in names:
            self.assertTrue(name.endswith(".dcm"), name)
            self.assertIn("cbct/raw", name)


    def test_the_panoramic_pngs_still_reach_the_export(self):
        """Decision #8, guarded where F13's fix could have broken it.

        The panoramic MIP and ray-sum are ordinary single-object rows, and the change
        that taught the exporter about prefix rows runs *before* the existence check
        every file entry used to take. A misclassification here would drop the two
        PNGs every existing export contains -- silently, since a skipped artifact is
        a warning. The roadmap asks for exactly this test by name (risk 15).
        """
        from common.export_catalog import artifact_by_key
        from common.models import FileRegistry

        processor = ExportProcessor.__new__(ExportProcessor)
        for key, subtype, filename in (
            ("panoramic.mip", "mip", "panoramic_mip.png"),
            ("panoramic.raysum", "raysum", "panoramic_xray.png"),
        ):
            with self.subTest(artifact=key):
                path = f"life/derived/panoramic/{subtype}.png"
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


class SeriesAnnotationLockTests(DicomLifecycleBase):
    """The gap the FileRegistry-row lock cannot see."""

    def test_ingest_anchors_the_series_as_a_source_resource(self):
        series = self.ingest(2)
        resource = SourceResource.objects.get(kind="dicom_series")
        self.assertEqual(resource.series_instance_uid, series.series_instance_uid)
        self.assertEqual(resource.file_id, series.file_id)
        # Coordinates are only comparable within one frame of reference; without this
        # on the record two series from one study look interchangeable.
        self.assertEqual(resource.frame_of_reference_uid, series.frame_of_reference_uid)
        self.assertEqual(resource.content_hash, series.file.file_hash)

    def _annotate(self, origin):
        """Record one revision against the series, with the given origin.

        No items: the seal rides on ``record_revision``, exactly where
        ``ever_annotated`` does, because that is the moment the raw data stops being
        replaceable. Adding geometry here would test the item validators instead.
        """
        series = DicomSeries.objects.get()
        resource = SourceResource.objects.get(kind="dicom_series")
        annotation_set = get_or_create_set(self.patient, kind="volume_segmentation")
        attach_target(annotation_set, resource, role="volume")
        record_revision(annotation_set, origin=origin)
        return series

    def test_human_work_seals_the_series(self):
        self.ingest(2)
        series = self._annotate(AnnotationOrigin.MANUAL)
        series.refresh_from_db()
        self.assertIsNotNone(series.sealed_at)

        instance = series.instances.first()
        instance.instance_number = 99
        with self.assertRaises(SealedSeriesError):
            instance.save()

    def test_a_prediction_does_not_seal(self):
        """A machine guess must not stop a correction being ingested later."""
        self.ingest(2)
        series = self._annotate(AnnotationOrigin.PREDICTION)
        series.refresh_from_db()
        self.assertIsNone(series.sealed_at)

    def test_a_series_with_no_annotations_stays_open(self):
        series = self.ingest(2)
        self.assertIsNone(series.sealed_at)
        instance = series.instances.first()
        instance.instance_number = 99
        instance.save()  # no exception


class DiscardRawTests(DicomLifecycleBase):
    """Roadmap risk 10: the raw row *is* the viewer source for a DICOM modality."""

    def test_discard_raw_is_refused_once_the_modality_has_dicom(self):
        self.ingest(2)
        step = ProcessingStep(modality=self.modality, name="CBCT", slug="cbct-x", discard_raw=True)
        with self.assertRaises(ValidationError) as caught:
            step.clean()
        self.assertIn("empty viewer", str(caught.exception))

    def test_discard_raw_is_allowed_for_a_modality_with_no_dicom(self):
        step = ProcessingStep(modality=self.modality, name="CBCT", slug="cbct-y", discard_raw=True)
        step.clean()  # no exception

    def test_ingest_refuses_under_a_discard_raw_step(self):
        ProcessingStep.objects.create(
            modality=self.modality, name="CBCT", slug="cbct", discard_raw=True
        )
        with self.assertRaises(DicomIngestError) as caught:
            self.ingest(2)
        self.assertIn("discard raw", str(caught.exception))
        self.assertEqual(self.storage.objects, {})


class DeidentificationSweepTests(DicomLifecycleBase):
    """The standing check that de-identification stayed true."""

    def test_a_clean_store_reports_ok(self):
        self.ingest(3)
        with mock.patch("common.tasks.get_object_storage", return_value=self.storage):
            result = verify_dicom_deidentification()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["checked"], 3)
        self.assertEqual(SystemCheck.objects.get(name="dicom_deidentification").status, "ok")

    def test_an_instance_that_gained_phi_is_reported(self):
        series = self.ingest(1)
        instance = series.instances.first()
        # Simulate the drift this exists to catch: an instance rewritten outside the
        # ingest, carrying an element the whitelist does not emit.
        from pydicom import dcmread

        dataset = dcmread(io.BytesIO(self.storage.objects[instance.object_key]))
        dataset.InstitutionName = "Somewhere Hospital"
        buffer = io.BytesIO()
        dataset.save_as(buffer, enforce_file_format=True)
        self.storage.objects[instance.object_key] = buffer.getvalue()

        with mock.patch("common.tasks.get_object_storage", return_value=self.storage):
            result = verify_dicom_deidentification()
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["leaks"], 1)

    def test_the_check_records_the_pseudonym_but_never_the_leaked_value(self):
        series = self.ingest(1)
        instance = series.instances.first()
        from pydicom import dcmread

        dataset = dcmread(io.BytesIO(self.storage.objects[instance.object_key]))
        dataset.PatientAddress = "12 Sentinel Street"
        buffer = io.BytesIO()
        dataset.save_as(buffer, enforce_file_format=True)
        self.storage.objects[instance.object_key] = buffer.getvalue()

        with mock.patch("common.tasks.get_object_storage", return_value=self.storage):
            verify_dicom_deidentification()

        details = SystemCheck.objects.get(name="dicom_deidentification").details
        recorded = str(details)
        # Writing the offending value into a SystemCheck row would move the leak
        # rather than report it.
        self.assertNotIn("Sentinel Street", recorded)
        self.assertIn(instance.sop_instance_uid, recorded)
