from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from common.models import Invitation, Modality, Project, ProjectAccess
from .models import Folder, FolderAccess, Patient
from .views.auth import _repair_empty_invitation_codes
from .intraoral_teeth import _normalize_teeth_payload


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


@override_settings(SECURE_SSL_REDIRECT=False)
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
        ProjectAccess.objects.create(user=self.user, project=self.project, role='annotator')

        self.folder = Folder.objects.create(name='Cases', project=self.project)

    def _nii_gz_upload(self, name='volume.nii.gz'):
        return SimpleUploadedFile(
            name,
            b'fake nii gz content',
            content_type='application/octet-stream',
        )

    @patch('maxillo.file_utils.save_cbct_to_dataset')
    def test_web_upload_accepts_cbct_nii_gz(self, save_cbct):
        save_cbct.return_value = ('maxillo/raw/cbct/volume.nii.gz', SimpleNamespace(id=42))
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('maxillo:upload_patient'),
            data={
                'name': 'CBCT Patient',
                'project': str(self.project.id),
                'folder': str(self.folder.id),
                'cbct': self._nii_gz_upload(),
            },
        )

        self.assertEqual(response.status_code, 302)
        save_cbct.assert_called_once()
        patient = Patient.objects.get(name='CBCT Patient')
        self.assertEqual(patient.folder, self.folder)
        self.assertEqual(patient.project, self.project)
        self.assertIn(self.cbct, patient.modalities.all())

    @patch('maxillo.file_utils.save_cbct_to_dataset')
    def test_project_upload_api_accepts_cbct_nii_gz(self, save_cbct):
        save_cbct.return_value = ('maxillo/raw/cbct/volume.nii.gz', SimpleNamespace(id=43, status='pending'))
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('api:api_project_upload', kwargs={'project_slug': 'maxillo'}),
            data={
                'name': 'API CBCT Patient',
                'project': str(self.project.id),
                'folder': str(self.folder.id),
                'cbct': self._nii_gz_upload(),
            },
        )

        self.assertEqual(response.status_code, 200)
        save_cbct.assert_called_once()
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['patient']['upload_results']['jobs'][0]['type'], 'cbct')

    def test_project_upload_api_rejects_non_nii_gz_cbct(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('api:api_project_upload', kwargs={'project_slug': 'maxillo'}),
            data={
                'name': 'Invalid CBCT Patient',
                'project': str(self.project.id),
                'folder': str(self.folder.id),
                'cbct': SimpleUploadedFile('scan.dcm', b'raw dicom', content_type='application/dicom'),
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.json())
