"""Generic per-algorithm output registration (no code per new algo).

Covers the changes that let mark_job_completed register any algorithm's outputs
generically: get_file_type_for_modality no longer requires the computed
file_type to pre-exist in FILE_TYPE_CHOICES; the generic branch sets `subtype`
(per output key) + the `modality` FK; and cbct/ios/video now flow through that
same generic branch instead of their own dedicated write code, while legacy
rows (and every reader that hardcodes their exact old file_type strings) keep
working unchanged -- see maxillo.tests_completion_registration for the
old+new coexistence tests.
"""
from unittest import mock

from django.test import TestCase

from common.modality_config import _processed_exists_for
from common.models import FileRegistry, Job, Modality, ProcessingStep
from maxillo.file_utils import get_file_type_for_modality, mark_job_completed
from maxillo.models import Patient


class GetFileTypeForModalityNewSlugTests(TestCase):
    def test_unlisted_modality_gets_its_own_processed_type(self):
        self.assertEqual(
            get_file_type_for_modality("sn", is_processed=True), "sn_processed"
        )
        self.assertEqual(
            get_file_type_for_modality("sn", is_processed=False), "sn_raw"
        )

    def test_known_special_cases_unaffected(self):
        self.assertEqual(
            get_file_type_for_modality("bite_classification", is_processed=True),
            "bite_classification",
        )
        self.assertEqual(
            get_file_type_for_modality("rawzip", is_processed=True), "generic_processed"
        )
        self.assertEqual(
            get_file_type_for_modality("cbct", is_processed=True), "cbct_processed"
        )

    def test_ios_with_subtype_keeps_legacy_naming(self):
        # Every real caller of the ios path passes subtype explicitly (raw uploads).
        self.assertEqual(
            get_file_type_for_modality("ios", is_processed=True, subtype="upper"),
            "ios_processed_upper",
        )
        self.assertEqual(
            get_file_type_for_modality("ios", is_processed=False, subtype="lower"),
            "ios_raw_lower",
        )

    def test_ios_without_subtype_is_generic(self):
        # No real caller does this except mark_job_completed's generic branch.
        self.assertEqual(
            get_file_type_for_modality("ios", is_processed=True), "ios_processed"
        )


class MarkJobCompletedGenericRegistrationTests(TestCase):
    def setUp(self):
        patcher = mock.patch("common.signals.celery_app.send_task")
        self.mock_send_task = patcher.start()
        self.addCleanup(patcher.stop)

        exists_patcher = mock.patch(
            "maxillo.file_utils.artifact_exists", return_value=True
        )
        exists_patcher.start()
        self.addCleanup(exists_patcher.stop)

        # _size_hash_for_path_or_key swallows failures and returns (None, None),
        # but without this it burns several real seconds per call attempting a
        # network head() against a nonexistent object-storage endpoint.
        storage_patcher = mock.patch(
            "maxillo.file_utils.get_object_storage",
            return_value=mock.Mock(head=mock.Mock(side_effect=Exception("no storage in test"))),
        )
        storage_patcher.start()
        self.addCleanup(storage_patcher.stop)

        self.modality = Modality.objects.create(name="ScanNormalizer", slug="sn")
        self.step = ProcessingStep.objects.create(
            modality=self.modality, name="ScanNormalizer", slug="sn", algo_name="sn"
        )

    def test_multi_output_job_registers_one_row_per_output_with_subtype(self):
        job = Job.objects.create(
            domain="maxillo",
            modality_slug="sn",
            step=self.step,
            status="processing",
        )
        output_files = {
            "a_oriented.stl": "maxillo/processed/sn/job_%d/a_oriented.stl" % job.id,
            "b_oriented.stl": "maxillo/processed/sn/job_%d/b_oriented.stl" % job.id,
        }

        result = mark_job_completed(job.id, output_files)

        self.assertTrue(result)
        rows = FileRegistry.objects.filter(processing_job=job).order_by("subtype")
        self.assertEqual(rows.count(), 2)
        subtypes = sorted(r.subtype for r in rows)
        self.assertEqual(subtypes, ["a_oriented.stl", "b_oriented.stl"])
        for row in rows:
            self.assertEqual(row.file_type, "sn_processed")
            self.assertEqual(row.modality_id, self.modality.id)

    def test_single_output_job_registers_one_row(self):
        job = Job.objects.create(
            domain="maxillo",
            modality_slug="sn",
            step=self.step,
            status="processing",
        )
        output_files = {"only.stl": "maxillo/processed/sn/job_%d/only.stl" % job.id}

        mark_job_completed(job.id, output_files)

        row = FileRegistry.objects.get(processing_job=job)
        self.assertEqual(row.file_type, "sn_processed")
        self.assertEqual(row.subtype, "only.stl")
        self.assertEqual(row.modality_id, self.modality.id)


class LegacyModalityUnifiedRegistrationTests(TestCase):
    """cbct/ios/video now flow through the same generic branch as sn -- these
    prove existing views/readers still work for both legacy-format rows already
    in the database and the new generically-named rows going forward."""

    def setUp(self):
        patcher = mock.patch("common.signals.celery_app.send_task")
        patcher.start()
        self.addCleanup(patcher.stop)

        mock.patch("maxillo.file_utils.artifact_exists", return_value=True).start()
        self.addCleanup(mock.patch.stopall)

        mock.patch(
            "maxillo.file_utils.get_object_storage",
            return_value=mock.Mock(head=mock.Mock(side_effect=Exception("no storage in test"))),
        ).start()

        self.patient = Patient.objects.create(name="Test Patient")
        for slug, name in (("cbct", "CBCT"), ("ios", "IOS"), ("video", "Video")):
            modality = Modality.objects.create(name=name, slug=slug)
            ProcessingStep.objects.create(modality=modality, name=name, slug=slug)

    def _job(self, slug):
        return Job.objects.create(
            domain="maxillo", modality_slug=slug, patient=self.patient, status="processing"
        )

    def test_cbct_completion_single_row_and_get_cbct_processed_file_works(self):
        job = self._job("cbct")
        mark_job_completed(job.id, {"segmentation_nifti": "maxillo/processed/cbct/a.nii"})

        row = self.patient.get_cbct_processed_file()
        self.assertIsNotNone(row)
        self.assertEqual(row.file_type, "cbct_processed")
        self.assertEqual(row.file_path, "maxillo/processed/cbct/a.nii")

    def test_cbct_recompletion_replaces_stale_row(self):
        job1 = self._job("cbct")
        mark_job_completed(job1.id, {"segmentation_nifti": "maxillo/processed/cbct/a.nii"})
        job2 = self._job("cbct")
        mark_job_completed(job2.id, {"segmentation_nifti": "maxillo/processed/cbct/b.nii"})

        rows = FileRegistry.objects.filter(file_type="cbct_processed", patient=self.patient)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().file_path, "maxillo/processed/cbct/b.nii")
        # Single-object lookup (used by templates/views) must still resolve.
        self.assertEqual(self.patient.get_cbct_processed_file().file_path, "maxillo/processed/cbct/b.nii")

    def test_ios_legacy_and_new_rows_coexist(self):
        # Pre-existing row from before this change, in the old exact-string format.
        FileRegistry.objects.create(
            file_type="ios_processed_upper",
            file_path="legacy/upper.stl",
            file_size=1,
            file_hash="x",
            domain="maxillo",
            patient=self.patient,
        )

        job = self._job("ios")
        mark_job_completed(
            job.id,
            {
                "upper": "maxillo/processed/ios/job_%d/upper.stl" % job.id,
                "lower": "maxillo/processed/ios/job_%d/lower.stl" % job.id,
            },
        )

        # New rows use the generic naming.
        new_rows = FileRegistry.objects.filter(file_type="ios_processed", patient=self.patient)
        self.assertEqual(new_rows.count(), 2)
        # Legacy row is untouched (different file_type -> not caught by the dedup delete).
        self.assertTrue(
            FileRegistry.objects.filter(file_type="ios_processed_upper", patient=self.patient).exists()
        )

        # Both readers find rows regardless of old vs new format.
        self.assertTrue(self.patient.has_ios_scans())
        processed = self.patient.get_ios_processed_files()
        self.assertIsNotNone(processed["upper"])
        self.assertIsNotNone(processed["lower"])

    def test_ios_landmark_prediction_becomes_active_without_manual_landmarks(self):
        job = self._job("ios")
        landmark_path = "maxillo/processed/ios/job_%d/landmarks.json" % job.id

        mark_job_completed(job.id, {"landmarks.json": landmark_path})

        landmark = FileRegistry.objects.get(file_type="ios_landmarks", patient=self.patient)
        self.assertEqual(landmark.file_path, landmark_path)
        self.assertEqual(landmark.metadata["origin"], "ai")

    def test_ios_prediction_does_not_replace_manual_landmarks(self):
        FileRegistry.objects.create(
            file_type="ios_landmarks",
            file_path="maxillo/processed/ios/ios_landmarks_patient_%d.json" % self.patient.patient_id,
            file_size=1,
            file_hash="manual",
            metadata={"origin": "manual"},
            domain="maxillo",
            patient=self.patient,
        )
        job = self._job("ios")
        landmark_path = "maxillo/processed/ios/job_%d/landmarks.json" % job.id

        mark_job_completed(job.id, {"landmarks.json": landmark_path})

        active = FileRegistry.objects.get(file_type="ios_landmarks", patient=self.patient)
        self.assertEqual(active.metadata["origin"], "manual")
        prediction = FileRegistry.objects.get(file_type="ios_landmarks_prediction", patient=self.patient)
        self.assertEqual(prediction.file_path, landmark_path)

    def test_video_completion_unchanged_shape(self):
        job = self._job("video")
        mark_job_completed(
            job.id,
            {
                "compressed": "maxillo/processed/video/job_%d/c.mp4" % job.id,
                "subsampled": "maxillo/processed/video/job_%d/s.mp4" % job.id,
            },
        )
        rows = FileRegistry.objects.filter(file_type="video_processed", patient=self.patient)
        self.assertEqual(rows.count(), 2)
        subtypes = sorted(r.subtype for r in rows)
        self.assertEqual(subtypes, ["compressed", "subsampled"])

    def test_processed_exists_for_matches_old_and_new_ios_rows(self):
        raw_file = FileRegistry.objects.create(
            file_type="ios_raw_upper",
            file_path="raw/upper.stl",
            file_size=1,
            file_hash="x",
            domain="maxillo",
            patient=self.patient,
        )

        self.assertFalse(_processed_exists_for(raw_file, "ios"))

        FileRegistry.objects.create(
            file_type="ios_processed_upper",
            file_path="legacy/upper.stl",
            file_size=1,
            file_hash="x",
            domain="maxillo",
            patient=self.patient,
        )
        self.assertTrue(_processed_exists_for(raw_file, "ios"))

        FileRegistry.objects.filter(file_type="ios_processed_upper").delete()
        FileRegistry.objects.create(
            file_type="ios_processed",
            subtype="upper",
            file_path="new/upper.stl",
            file_size=1,
            file_hash="x",
            domain="maxillo",
            patient=self.patient,
        )
        self.assertTrue(_processed_exists_for(raw_file, "ios"))
