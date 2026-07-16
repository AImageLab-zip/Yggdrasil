from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.test import TestCase
from django.test import override_settings
from django.test.client import RequestFactory
from django.urls import reverse
from django.utils import timezone

from common.models import FileRegistry, Invitation, Job, Modality, Project, ProjectAccess
from .file_utils import mark_job_completed, save_cbct_to_dataset
from .models import Folder, FolderAccess, Patient
from .views.auth import _repair_empty_invitation_codes
from .views.admin import rerun_processing
from .views.intraoral_segmentation import _normalize_teeth_payload
from .views.patient_data import _generated_panoramic_variants


class IntraoralSegmentationNormalizationTests(SimpleTestCase):
    def test_accepts_legacy_single_polygon_shape(self):
        payload = {
            '11': [[1, 2], [3, 4], [5, 6]],
        }

        normalized = _normalize_teeth_payload(payload, image_bounds=(10, 10))

        self.assertEqual(normalized['11'], [[[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]])

    def test_preserves_multiple_polygons_for_one_tooth(self):
        payload = {
            '11': [
                [[1, 2], [3, 4], [5, 6]],
                [[6, 5], [8, 5], [7, 7]],
            ],
        }

        normalized = _normalize_teeth_payload(payload, image_bounds=(10, 10))

        self.assertEqual(len(normalized['11']), 2)
        self.assertEqual(normalized['11'][1], [[6.0, 5.0], [8.0, 5.0], [7.0, 7.0]])

    def test_rejects_points_outside_image_bounds(self):
        payload = {
            '11': [[1, 2], [3, 4], [11, 6]],
        }

        with self.assertRaisesMessage(ValueError, 'Point coordinates must stay inside image bounds.'):
            _normalize_teeth_payload(payload, image_bounds=(10, 10))


class InvitationCodeTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='Test Project')

    def test_save_generates_missing_code(self):
        invitation = Invitation.objects.create(
            code='',
            project=self.project,
            expires_at=timezone.now() + timedelta(days=7),
        )

        self.assertTrue(invitation.code)

    def test_repair_empty_invitation_codes_updates_existing_rows(self):
        Invitation.objects.bulk_create([
            Invitation(
                code='',
                project=self.project,
                expires_at=timezone.now() + timedelta(days=7),
            )
        ])

        _repair_empty_invitation_codes()

        invitation = Invitation.objects.get()
        self.assertTrue(invitation.code)


class CbctDependencyJobTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.get_or_create(
            slug='maxillo',
            defaults={'name': 'maxillo'},
        )
        self.patient = Patient.objects.create(name='CBCT Case')

    @override_settings(
        RUNNER_QUEUE_BY_MODALITY={
            'cbct': 'runner_cbct_test',
            'cbct_to_panoramic': 'runner_cbct_to_panoramic_test',
        },
    )
    @patch('common.signals.celery_app.send_task')
    @patch('maxillo.file_utils._upload_uploaded_file_to_storage')
    def test_cbct_upload_creates_segmentation_job_and_dependent_panoramic_job(
        self,
        upload_file,
        send_task,
    ):
        upload_file.return_value = (
            'maxillo/raw/cbct/cbct_patient_1.nii.gz',
            123,
            'sha256',
        )
        uploaded = SimpleUploadedFile('case.nii.gz', b'nifti', content_type='application/gzip')

        path, job = save_cbct_to_dataset(self.patient, uploaded)

        self.assertEqual(path, 'maxillo/raw/cbct/cbct_patient_1.nii.gz')
        self.assertEqual(job.modality_slug, 'cbct')
        panoramic_job = Job.objects.get(modality_slug='cbct_to_panoramic')
        self.assertEqual(panoramic_job.status, 'dependency')
        self.assertEqual(panoramic_job.input_files, {'raw_cbct': path})
        self.assertEqual(list(panoramic_job.dependencies.all()), [job])
        send_task.assert_called_once()

    @override_settings(
        RUNNER_QUEUE_BY_MODALITY={
            'cbct': 'runner_cbct_test',
            'cbct_to_panoramic': 'runner_cbct_to_panoramic_test',
        },
    )
    @patch('common.signals.celery_app.send_task')
    @patch('maxillo.file_utils._size_hash_for_path_or_key', return_value=(456, 'etag'))
    @patch('maxillo.file_utils.artifact_exists', return_value=True)
    def test_cbct_completion_updates_and_releases_panoramic_job(
        self,
        artifact_exists,
        size_hash,
        send_task,
    ):
        raw_key = 'maxillo/raw/cbct/cbct_patient_1.nii.gz'
        seg_key = 'maxillo/processed/cbct/job_1/segmentation.nii.gz'
        cbct_job = Job.objects.create(
            modality_slug='cbct',
            status='processing',
            patient=self.patient,
            input_files={'input': raw_key},
        )
        panoramic_job = Job.objects.create(
            modality_slug='cbct_to_panoramic',
            status='dependency',
            patient=self.patient,
            input_files={'raw_cbct': raw_key},
        )
        panoramic_job.add_dependency(cbct_job)

        self.assertTrue(
            mark_job_completed(
                cbct_job.id,
                {'segmentation_nifti': {'path': seg_key}},
            )
        )

        panoramic_job.refresh_from_db()
        self.assertEqual(panoramic_job.status, 'pending')
        self.assertEqual(
            panoramic_job.input_files,
            {'raw_cbct': raw_key, 'segmentation_nifti': seg_key},
        )
        send_task.assert_called_once()

    @patch('maxillo.file_utils._size_hash_for_path_or_key', return_value=(456, 'etag'))
    @patch('maxillo.file_utils.artifact_exists', return_value=True)
    def test_panoramic_completion_registers_all_projection_variants(
        self,
        artifact_exists,
        size_hash,
    ):
        outputs = {
            'panoramic_png': {'path': 'maxillo/processed/cbct_to_panoramic/job_1/z0_mean.png'},
            'panoramic_zminus20_mean_png': {'path': 'maxillo/processed/cbct_to_panoramic/job_1/zminus20_mean.png'},
            'panoramic_z0_raysum_png': {'path': 'maxillo/processed/cbct_to_panoramic/job_1/z0_raysum.png'},
            'panoramic_zminus20_raysum_png': {'path': 'maxillo/processed/cbct_to_panoramic/job_1/zminus20_raysum.png'},
        }
        job = Job.objects.create(
            modality_slug='cbct_to_panoramic',
            status='processing',
            patient=self.patient,
        )

        self.assertTrue(mark_job_completed(job.id, outputs))

        panoramic_files = FileRegistry.objects.filter(processing_job=job)
        self.assertEqual(panoramic_files.count(), 4)
        default_file = panoramic_files.get(metadata__is_default=True)
        self.assertEqual(default_file.file_path, outputs['panoramic_png']['path'])
        self.assertEqual(set(default_file.metadata['files']), set(outputs))


class PanoramicVariantTests(SimpleTestCase):
    def test_returns_only_known_generated_variants(self):
        variants = _generated_panoramic_variants(
            SimpleNamespace(
                metadata={
                    'generated_from': 'cbct_to_panoramic',
                    'files': {
                        'panoramic_png': {'path': 'z0_mean.png'},
                        'panoramic_z0_raysum_png': {'path': 'z0_raysum.png'},
                        'unexpected': {'path': 'ignored.png'},
                    },
                }
            )
        )
        self.assertEqual(
            variants,
            {
                'z0_mean': {'path': 'z0_mean.png', 'label': 'Z+0 Mean'},
                'z0_raysum': {'path': 'z0_raysum.png', 'label': 'Z+0 Raysum'},
            },
        )


class PanoramicRerunTests(TestCase):
    def setUp(self):
        self.patient = Patient.objects.create(name='Panoramic Case')
        self.user = User.objects.create_user(
            username='panoramic-admin', password='x', is_staff=True,
        )
        self.old_job = Job.objects.create(
            modality_slug='cbct_to_panoramic',
            status='completed',
            patient=self.patient,
        )
        self.job = Job.objects.create(
            modality_slug='cbct_to_panoramic',
            status='completed',
            patient=self.patient,
            output_files={'panoramic_png': {'path': 'maxillo/processed/current.png'}},
        )
        self.generated_paths = [
            'maxillo/processed/current.png',
            'maxillo/processed/old.png',
        ]
        for path, job in zip(self.generated_paths, [self.job, self.old_job]):
            FileRegistry.objects.create(
                file_type='panoramic_processed',
                file_path=path,
                file_size=1,
                file_hash='hash',
                patient=self.patient,
                processing_job=job,
                metadata={'generated_from': 'cbct_to_panoramic'},
            )
        self.manual_file = FileRegistry.objects.create(
            file_type='panoramic_processed',
            file_path='maxillo/raw/manual.png',
            file_size=1,
            file_hash='manual',
            patient=self.patient,
        )

    @patch('common.signals.celery_app.send_task')
    @patch('maxillo.views.admin.get_object_storage')
    def test_panorama_rerun_clears_generated_artifacts_and_queues_worker_job(
        self,
        storage_factory,
        send_task,
    ):
        request = RequestFactory().post(
            f'/maxillo/patient/{self.patient.patient_id}/rerun-processing/',
            data=json.dumps({'jobs': ['panoramic']}),
            content_type='application/json',
        )
        request.user = self.user
        request.resolver_match = SimpleNamespace(namespace='maxillo')

        response = rerun_processing(request, self.patient.patient_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)['updated'], ['panoramic'])
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'pending')
        self.assertEqual(self.job.output_files, {})
        self.assertFalse(
            FileRegistry.objects.filter(
                metadata__generated_from='cbct_to_panoramic',
            ).exists()
        )
        self.assertTrue(FileRegistry.objects.filter(id=self.manual_file.id).exists())
        self.assertEqual(
            storage_factory.return_value.delete.call_args_list,
            [((path,),) for path in self.generated_paths],
        )

class MaxilloCbctFolderUploadTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.get_or_create(
            slug='maxillo',
            defaults={'name': 'maxillo'},
        )
        self.cbct, _ = Modality.objects.get_or_create(
            slug='cbct',
            defaults={'name': 'CBCT'},
        )
        self.project.modalities.add(self.cbct)

        self.user = User.objects.create_user(username='uploader', password='x')
        ProjectAccess.objects.create(user=self.user, project=self.project, role='standard')

        self.folder = Folder.objects.create(name='Cases')
        FolderAccess.objects.create(user=self.user, folder=self.folder, role='annotator')

    def _dicom_upload(self):
        return SimpleUploadedFile(
            'slice1.dcm',
            b'DICM test content',
            content_type='application/dicom',
        )

    @patch('maxillo.file_utils.save_cbct_folder_to_dataset')
    def test_web_upload_accepts_cbct_folder(self, save_cbct_folder):
        save_cbct_folder.return_value = ('maxillo/raw/cbct/folder', SimpleNamespace(id=42))
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('maxillo:upload_patient'),
            data={
                'name': 'Folder CBCT',
                'folder': str(self.folder.id),
                'cbct_upload_type': 'folder',
                'cbct_folder_files': [self._dicom_upload()],
            },
        )

        self.assertEqual(response.status_code, 302)
        save_cbct_folder.assert_called_once()
        patient = Patient.objects.get(name='Folder CBCT')
        self.assertEqual(patient.folder, self.folder)
        self.assertIn(self.cbct, patient.modalities.all())

    @patch('maxillo.file_utils.save_cbct_folder_to_dataset')
    def test_project_upload_api_accepts_cbct_folder(self, save_cbct_folder):
        save_cbct_folder.return_value = ('maxillo/raw/cbct/folder', SimpleNamespace(id=43, status='pending'))
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('api:api_project_upload', kwargs={'project_slug': 'maxillo'}),
            data={
                'name': 'API Folder CBCT',
                'folder': str(self.folder.id),
                'cbct_upload_type': 'folder',
                'cbct_folder_files': [self._dicom_upload()],
            },
        )

        self.assertEqual(response.status_code, 200)
        save_cbct_folder.assert_called_once()
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['patient']['upload_results']['jobs'][0]['type'], 'cbct')
