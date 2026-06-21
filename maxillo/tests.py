from datetime import timedelta

from django.test import SimpleTestCase
from django.test import TestCase
from django.utils import timezone

from common.models import Invitation, Project
from .views.auth import _repair_empty_invitation_codes
from .views.intraoral_segmentation import _normalize_teeth_payload


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
