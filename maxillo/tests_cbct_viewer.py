from unittest.mock import patch

from django.test import TestCase

from common.models import FileRegistry, Job
from maxillo.file_utils import mark_job_completed
from maxillo.models import Patient
from maxillo.views.patient_detail import _cbct_display_file, _cbct_segmentation_file


class CbctProcessedBundleTests(TestCase):
    def setUp(self):
        self.patient = Patient.objects.create(name='CBCT Viewer Case')

    @patch('maxillo.file_utils._size_hash_for_path_or_key', return_value=(100, 'etag'))
    @patch('maxillo.file_utils.artifact_exists', return_value=True)
    def test_completion_registers_paired_volume_and_segmentation(self, artifact_exists, size_hash):
        job = Job.objects.create(
            modality_slug='cbct',
            status='processing',
            patient=self.patient,
            input_files={'input': 'maxillo/raw/cbct/source.nii.gz'},
        )

        self.assertTrue(mark_job_completed(job.id, {
            'volume_nifti': {'path': 'maxillo/processed/cbct/job_1/volume.nii.gz'},
            'segmentation_nifti': {'path': 'maxillo/processed/cbct/job_1/segmentation.nii.gz'},
            'inference_stats_json': {'path': 'maxillo/processed/cbct/job_1/inference_stats.json'},
        }))

        bundle = FileRegistry.objects.get(file_type='cbct_processed')
        self.assertEqual(
            set(bundle.metadata['files']),
            {'volume_nifti', 'segmentation_nifti', 'inference_stats_json'},
        )
        self.assertEqual(bundle.processing_job, job)


class CbctViewerConfigTests(TestCase):
    def setUp(self):
        self.patient = Patient.objects.create(name='CBCT Viewer Config')
        self.job = Job.objects.create(
            modality_slug='cbct',
            status='completed',
            patient=self.patient,
            input_files={'input': 'maxillo/raw/cbct/source.nii.gz'},
        )

    def _processed(self, files):
        primary = files.get('segmentation_nifti') or files.get('volume_nifti')
        return FileRegistry.objects.create(
            file_type='cbct_processed',
            file_path=primary['path'],
            file_size=100,
            file_hash='multi-file',
            patient=self.patient,
            processing_job=self.job,
            metadata={'files': files},
        )

    @patch('maxillo.views.patient_detail.artifact_exists', return_value=True)
    def test_new_scan_exposes_optional_segmentation(self, artifact_exists):
        processed = self._processed({
            'volume_nifti': {'path': 'maxillo/processed/cbct/job_1/volume.nii.gz'},
            'segmentation_nifti': {'path': 'maxillo/processed/cbct/job_1/segmentation.nii.gz'},
        })

        segmentation = _cbct_segmentation_file(self.patient)

        self.assertEqual(segmentation, {
            'id': processed.id,
            'fileKey': 'segmentation_nifti',
            'labelMax': 98,
        })

    @patch('maxillo.views.patient_detail.artifact_exists', return_value=True)
    def test_historical_nifti_remains_primary_display_file(self, artifact_exists):
        raw = FileRegistry.objects.create(
            file_type='cbct_raw',
            file_path='maxillo/raw/cbct/source.nii.gz',
            file_size=100,
            file_hash='raw-hash',
            patient=self.patient,
        )
        self._processed({
            'segmentation_nifti': {'path': 'maxillo/processed/cbct/job_1/segmentation.nii.gz'},
        })

        display_file, file_key = _cbct_display_file(self.patient)

        self.assertEqual(display_file, raw)
        self.assertEqual(file_key, 'primary')

    @patch('maxillo.views.patient_detail.artifact_exists', return_value=True)
    def test_processed_volume_displays_without_segmentation(self, artifact_exists):
        processed = self._processed({
            'volume_nifti': {'path': 'maxillo/processed/cbct/job_1/volume.nii.gz'},
        })

        display_file, file_key = _cbct_display_file(self.patient)

        self.assertEqual(display_file, processed)
        self.assertEqual(file_key, 'volume_nifti')
        self.assertIsNone(_cbct_segmentation_file(self.patient))
