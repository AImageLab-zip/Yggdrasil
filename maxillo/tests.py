from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from common.models import Invitation, Modality, Project, ProjectAccess
from .models import Folder, Patient
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



@override_settings(
    SECURE_SSL_REDIRECT=False,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    EMAIL_SENDER_EMAILS=['noreply@example.org'],
    DEFAULT_FROM_EMAIL='noreply@example.org',
)
class InvitationEmailTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='Dental & Co')
        self.staff = User.objects.create_user('staff', password='pw', is_staff=True)
        self.user = User.objects.create_user('plain', password='pw')

    def _create(self, **extra):
        self.client.force_login(self.staff)
        data = {
            'email': 'new.user@example.org',
            'sender_email': 'noreply@example.org',
            'role': 'annotator',
            'projects': [self.project.pk],
            'expiry_days': 7,
            'signature': '',
        }
        data.update(extra)
        return self.client.post(reverse('invitation_list'), data)

    def test_invitation_email_is_html_with_inline_logo(self):
        response = self._create()

        self.assertRedirects(response, reverse('invitation_list'), fetch_redirect_response=False)
        invitation = Invitation.objects.get()
        self.assertIsNotNone(invitation.email_sent_at)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ['new.user@example.org'])
        self.assertEqual(message.from_email, 'noreply@example.org')

        register_url = f"http://testserver{reverse('register')}?code={invitation.code}"
        self.assertIn(register_url, message.body)
        self.assertIn('Dental & Co', message.body)  # text part is not HTML-escaped

        html, mimetype = message.alternatives[0]
        self.assertEqual(mimetype, 'text/html')
        self.assertIn(f'href="{register_url}"', html)
        self.assertIn('src="cid:yggdrasil-logo"', html)
        self.assertIn('Dental &amp; Co', html)

        parsed = message.message()
        self.assertEqual(parsed.get_content_subtype(), 'related')
        logo = [part for part in parsed.walk() if part.get('Content-ID') == '<yggdrasil-logo>']
        self.assertEqual(len(logo), 1)
        self.assertEqual(logo[0].get_content_type(), 'image/png')

    def test_invitation_without_email_sends_nothing(self):
        self._create(email='')

        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(Invitation.objects.count(), 1)

    def test_invitation_page_is_staff_only(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('invitation_list'))

        self.assertEqual(response.status_code, 302)

    def test_invitation_page_lists_users_with_emails(self):
        self.user.email = 'plain@example.org'
        self.user.save()
        ProjectAccess.objects.create(user=self.user, project=self.project, role='annotator')
        User.objects.create_user('gone', email='gone@example.org', is_active=False)
        self.client.force_login(self.staff)

        response = self.client.get(reverse('invitation_list'))

        self.assertContains(response, 'href="mailto:plain@example.org"')
        self.assertContains(response, 'Dental &amp; Co · Annotator')
        self.assertContains(response, 'Inactive')
        # "Copy all emails" only gathers active users.
        self.assertEqual(response.context['contact_emails'], 'plain@example.org')

    def test_delete_invitation_is_a_post_form(self):
        self._create(email='')
        invitation = Invitation.objects.get()
        url = reverse('delete_invitation', args=[invitation.code])

        page = self.client.get(reverse('invitation_list'))
        self.assertContains(page, f'<form method="post" action="{url}"')
        self.client.post(url)

        self.assertFalse(Invitation.objects.exists())

    def test_rail_links_invitations_for_staff_only(self):
        url = reverse('invitation_list')

        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse('admin_control_panel')), f'href="{url}"')

        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(reverse("changelog_page")), f'href="{url}"')


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
                'cbct': SimpleUploadedFile('scan.mha', b'not a nifti', content_type='application/octet-stream'),
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.json())
