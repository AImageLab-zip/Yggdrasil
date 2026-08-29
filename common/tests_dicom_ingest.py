"""Ingest: a DICOM folder is stored as DICOM, and cataloged.

The tests here assert against the **bytes that reached object storage**, through an
in-memory fake, rather than against the dataset the code held in memory. That is the
distinction the whole phase turns on: what a user is exposed to is what was written,
and a de-identifier that is correct right up until it serialises is not correct.
"""

import io
from unittest import mock

from django.test import TestCase, override_settings
from pydicom import dcmread

from common.dicom.deidentify import ANONYMOUS_NAME, pseudonymous_uid
from common.dicom.ingest import DEID_PROFILE, DicomIngestError, ingest_dicom_series
from common.dicom.models import DicomInstance, DicomSeries, SealedSeriesError
from common.models import FileRegistry, Project
from common.tests_dicom import PHI_MARKERS, synthetic_instance
from maxillo.models import Folder, Patient


class FakeStorage:
    """Records every object written, so a test can read back the real bytes."""

    def __init__(self):
        self.objects = {}

    def upload_fileobj(self, fileobj, *, key, content_type=None, metadata=None):
        self.objects[key] = fileobj.read()
        return mock.Mock(content_length=len(self.objects[key]))


def as_upload(dataset, name=None):
    """Serialise a synthetic dataset into something that quacks like an upload."""
    buffer = io.BytesIO()
    dataset.save_as(buffer, enforce_file_format=True)
    buffer.seek(0)
    buffer.name = name or f"{dataset.SOPInstanceUID}.dcm"
    return buffer


def series_of(count=3, *, series_uid="1.2.826.0.1.3680043.9.7.1", with_phi=True):
    return [
        as_upload(
            synthetic_instance(
                sop_instance_uid=f"{series_uid}.{index + 1}",
                series_instance_uid=series_uid,
                instance_number=index + 1,
                position=(0.0, 0.0, float(index)),
                with_phi=with_phi,
            )
        )
        for index in range(count)
    ]


@override_settings(DICOM_UID_HMAC_KEY="ingest-test-key")
class DicomIngestTests(TestCase):
    def setUp(self):
        project = Project.objects.create(name="dcm", slug="dcm", domain="maxillo")
        self.patient = Patient.objects.create(
            project=project, folder=Folder.objects.create(name="F", project=project)
        )
        self.storage = FakeStorage()
        patcher = mock.patch(
            "common.dicom.ingest.get_object_storage", return_value=self.storage
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def ingest(self, files):
        return ingest_dicom_series(
            self.patient, modality_slug="cbct", file_type="cbct_raw", files=files
        )

    # --- the property that matters -------------------------------------------------

    def test_no_phi_reaches_object_storage(self):
        self.ingest(series_of(3))
        self.assertEqual(len(self.storage.objects), 3)
        for key, payload in self.storage.objects.items():
            text = payload.decode("latin-1")
            for keyword, marker in PHI_MARKERS.items():
                self.assertNotIn(marker, text, f"{keyword} reached storage in {key}")

    def test_stored_bytes_are_readable_dicom_with_the_pixels_intact(self):
        self.ingest(series_of(1))
        payload = next(iter(self.storage.objects.values()))
        stored = dcmread(io.BytesIO(payload))
        self.assertEqual(stored.Modality, "CT")
        self.assertEqual(stored.Rows, 4)
        self.assertEqual(len(stored.PixelData), 4 * 4 * 2)
        self.assertEqual(stored.PatientName, ANONYMOUS_NAME)

    def test_geometry_survives_so_the_volume_can_still_be_built(self):
        self.ingest(series_of(3))
        positions = sorted(
            dcmread(io.BytesIO(payload)).ImagePositionPatient[2]
            for payload in self.storage.objects.values()
        )
        self.assertEqual(positions, [0.0, 1.0, 2.0])

    # --- the catalog ----------------------------------------------------------------

    def test_one_file_registry_row_per_series_with_a_prefix_path(self):
        [series] = self.ingest(series_of(3))
        row = series.file
        self.assertEqual(row.file_type, "cbct_raw")
        self.assertEqual(row.patient_id, self.patient.pk)
        # A prefix, not an object: every stored instance lives under it.
        for key in self.storage.objects:
            self.assertTrue(key.startswith(row.file_path + "/"))
        self.assertEqual(FileRegistry.objects.count(), 1)

    def test_the_registry_metadata_matches_the_folder_upload_shape(self):
        [series] = self.ingest(series_of(3))
        metadata = series.file.metadata
        self.assertEqual(metadata["file_count"], 3)
        self.assertEqual(metadata["input_type"], "dicom_series")
        self.assertEqual(len(metadata["files"]), 3)
        # Same four keys save_generic_modality_folder writes, so every existing
        # consumer of a prefix row reads a series without knowing what it is.
        self.assertEqual(
            sorted(metadata["files"][0]), ["hash", "name", "path", "size"]
        )

    def test_instances_are_cataloged_with_their_keys_and_geometry(self):
        [series] = self.ingest(series_of(3))
        self.assertEqual(series.instance_count, 3)
        instances = list(series.instances.all())
        self.assertEqual(len(instances), 3)
        self.assertEqual([i.instance_number for i in instances], [1, 2, 3])
        for instance in instances:
            self.assertIn(instance.object_key, self.storage.objects)
            self.assertEqual(len(instance.image_orientation_patient), 6)

    def test_the_series_records_what_deidentification_established(self):
        [series] = self.ingest(series_of(1))
        self.assertEqual(series.deid_profile, DEID_PROFILE)
        # The synthetic instance carries no BurnedInAnnotation, which is the common
        # case and establishes nothing about the pixels.
        self.assertEqual(series.deid_confidence, "header_only")

    def test_stored_uids_are_pseudonyms_not_the_originals(self):
        original = "1.2.826.0.1.3680043.9.7.1"
        [series] = self.ingest(series_of(2, series_uid=original))
        self.assertEqual(series.series_instance_uid, pseudonymous_uid(original))
        self.assertNotIn(original, series.series_instance_uid)
        for instance in series.instances.all():
            self.assertNotIn(original, instance.sop_instance_uid)

    def test_two_series_in_one_upload_become_two_rows(self):
        files = series_of(2, series_uid="1.2.3.10") + series_of(2, series_uid="1.2.3.20")
        created = self.ingest(files)
        self.assertEqual(len(created), 2)
        self.assertEqual(FileRegistry.objects.count(), 2)
        self.assertEqual(DicomInstance.objects.count(), 4)

    # --- refusals: nothing is written ------------------------------------------------

    def test_a_burned_in_instance_refuses_the_whole_upload(self):
        files = series_of(2)
        files.append(as_upload(synthetic_instance(
            sop_instance_uid="1.2.3.99", instance_number=9, burned_in="YES"
        )))
        with self.assertRaises(DicomIngestError) as caught:
            self.ingest(files)
        self.assertIn("burned-in", str(caught.exception))
        # Whole or not at all: a half-stored study is worse than a refused one.
        self.assertEqual(self.storage.objects, {})
        self.assertEqual(FileRegistry.objects.count(), 0)
        self.assertEqual(DicomSeries.objects.count(), 0)

    def test_a_secondary_capture_refuses_the_whole_upload(self):
        files = [as_upload(synthetic_instance(
            sop_class_uid="1.2.840.10008.5.1.4.1.1.7"
        ))]
        with self.assertRaises(DicomIngestError):
            self.ingest(files)
        self.assertEqual(self.storage.objects, {})

    def test_every_refusal_is_reported_in_one_pass(self):
        files = [
            as_upload(synthetic_instance(sop_instance_uid="1.2.3.1", burned_in="YES")),
            as_upload(synthetic_instance(
                sop_instance_uid="1.2.3.2", sop_class_uid="1.2.840.10008.5.1.4.1.1.7"
            )),
        ]
        with self.assertRaises(DicomIngestError) as caught:
            self.ingest(files)
        self.assertEqual(len(caught.exception.reasons), 2)

    def test_an_upload_with_no_dicom_in_it_says_so(self):
        junk = io.BytesIO(b"not a dicom file at all")
        junk.name = "readme.txt"
        with self.assertRaises(DicomIngestError) as caught:
            self.ingest([junk])
        self.assertIn("No DICOM images", str(caught.exception))

    def test_non_dicom_files_beside_a_real_series_are_ignored(self):
        junk = io.BytesIO(b"burned disc autorun stub")
        junk.name = "autorun.inf"
        [series] = self.ingest(series_of(2) + [junk])
        self.assertEqual(series.instance_count, 2)

    def test_a_dicomdir_index_is_skipped_not_stored(self):
        # A DICOMDIR is normal on a burned disc and is an index of patient directory
        # records -- exactly what this phase exists not to store.
        dicomdir = synthetic_instance(sop_instance_uid="1.2.3.500")
        dicomdir.SOPClassUID = "1.2.840.10008.1.3.10"
        dicomdir.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.1.3.10"
        [series] = self.ingest(series_of(2) + [as_upload(dicomdir, "DICOMDIR")])
        self.assertEqual(series.instance_count, 2)

    def test_reuploading_the_same_series_is_refused_rather_than_duplicated(self):
        self.ingest(series_of(2))
        with self.assertRaises(DicomIngestError) as caught:
            self.ingest(series_of(2))
        self.assertIn("already stored", str(caught.exception))
        self.assertEqual(DicomSeries.objects.count(), 1)


@override_settings(DICOM_UID_HMAC_KEY="ingest-test-key")
class SealedSeriesTests(TestCase):
    """The gap the FileRegistry-row lock cannot see."""

    def setUp(self):
        project = Project.objects.create(name="seal", slug="seal", domain="maxillo")
        self.patient = Patient.objects.create(
            project=project, folder=Folder.objects.create(name="F", project=project)
        )
        self.storage = FakeStorage()
        patcher = mock.patch(
            "common.dicom.ingest.get_object_storage", return_value=self.storage
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        [self.series] = ingest_dicom_series(
            self.patient, modality_slug="cbct", file_type="cbct_raw",
            files=series_of(2),
        )

    def test_an_unsealed_series_accepts_an_instance_rewrite(self):
        instance = self.series.instances.first()
        instance.instance_number = 7
        instance.save()
        self.assertEqual(DicomInstance.objects.get(pk=instance.pk).instance_number, 7)

    def test_a_sealed_series_refuses_a_rewrite(self):
        self.series.seal()
        instance = self.series.instances.first()
        instance.instance_number = 7
        with self.assertRaises(SealedSeriesError):
            instance.save()

    def test_a_sealed_series_refuses_a_new_instance(self):
        # An annotation drawn on 2 slices does not describe 3.
        self.series.seal()
        with self.assertRaises(SealedSeriesError):
            DicomInstance.objects.create(
                series=self.series, sop_instance_uid="1.2.3.new",
                object_key="somewhere/new.dcm",
            )

    def test_sealing_is_monotonic(self):
        first = self.series.seal()
        self.series.seal()
        self.series.refresh_from_db()
        self.assertEqual(self.series.sealed_at, first)
