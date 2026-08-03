import json
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase
from django.urls import reverse

from common.models import Project, ProjectAccess

from .models import Classification, Folder, Patient
from .views.patient_detail import patient_detail


class BiteClassificationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='classification-admin',
            password='x',
            is_staff=True,
        )
        self.project, _ = Project.objects.get_or_create(
            slug='maxillo',
            defaults={'name': 'Maxillo'},
        )
        ProjectAccess.objects.create(user=self.user, project=self.project, role='admin')
        self.folder = Folder.objects.create(name='Classification cases')
        self.patient = Patient.objects.create(name='Bite case', folder=self.folder)
        self.pipeline = Classification.objects.create(
            patient=self.patient,
            classifier='pipeline',
            sagittal_left='I',
            sagittal_right='II_edge',
            vertical='normal',
            transverse='normal',
            midline='centered',
        )
        self.client.force_login(self.user)

    def _update(self, field, value):
        return self.client.post(
            reverse('maxillo:update_classification', args=[self.patient.patient_id]),
            data=json.dumps({'field': field, 'value': value}),
            content_type='application/json',
        )

    def _confirm(self):
        request = RequestFactory().post(
            reverse('maxillo:patient_detail', args=[self.patient.patient_id]),
            data={'action': 'accept_ai'},
        )
        request.user = self.user
        request.user.profile = SimpleNamespace()
        request.resolver_match = SimpleNamespace(namespace='maxillo')
        request.session = {}
        request._messages = FallbackStorage(request)
        return patient_detail(request, self.patient.patient_id)

    def test_first_edit_copies_pipeline_and_changes_selected_field(self):
        response = self._update('vertical', 'deep')

        self.assertEqual(response.status_code, 200)
        manual = Classification.objects.get(patient=self.patient, classifier='manual')
        self.assertEqual(manual.vertical, 'deep')
        self.assertEqual(manual.sagittal_left, self.pipeline.sagittal_left)
        self.assertEqual(manual.sagittal_right, self.pipeline.sagittal_right)
        self.assertEqual(manual.annotator, self.user)

    def test_edit_after_confirmation_updates_same_manual_row(self):
        self._confirm()

        response = self._update('transverse', 'cross')

        self.assertEqual(response.status_code, 200)
        manual_rows = Classification.objects.filter(patient=self.patient, classifier='manual')
        self.assertEqual(manual_rows.count(), 1)
        self.assertEqual(manual_rows.get().transverse, 'cross')

    def test_confirmation_is_idempotent_and_preserves_manual_edits(self):
        self._update('vertical', 'open')

        response = self._confirm()

        self.assertEqual(response.status_code, 302)
        manual_rows = Classification.objects.filter(patient=self.patient, classifier='manual')
        self.assertEqual(manual_rows.count(), 1)
        self.assertEqual(manual_rows.get().vertical, 'open')

    def test_repeated_confirmation_creates_only_one_manual_row(self):
        self._confirm()
        self._confirm()

        manual_rows = Classification.objects.filter(patient=self.patient, classifier='manual')
        self.assertEqual(manual_rows.count(), 1)
        self.assertEqual(manual_rows.get().vertical, self.pipeline.vertical)

    def test_rejects_value_outside_model_choices(self):
        response = self._update('vertical', 'invalid')

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            Classification.objects.filter(patient=self.patient, classifier='manual').exists()
        )

    def test_confirm_button_is_hidden_after_manual_classification_exists(self):
        manual = Classification.objects.create(
            patient=self.patient,
            classifier='manual',
            sagittal_left='I',
            sagittal_right='II_edge',
            vertical='open',
            transverse='cross',
            midline='deviated',
            annotator=self.user,
        )

        html = render_to_string(
            'maxillo/sections/bite_classification_section.html',
            {
                'patient': self.patient,
                'ai_classification': self.pipeline,
                'manual_classification': manual,
                'can_modify_segmentation': True,
            },
        )

        self.assertNotIn('id="confirmReview"', html)

    def test_database_prevents_duplicate_classifier_for_patient(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Classification.objects.create(
                patient=self.patient,
                classifier='pipeline',
                sagittal_left='I',
                sagittal_right='I',
                vertical='normal',
                transverse='normal',
                midline='centered',
            )
