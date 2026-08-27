"""Calibrating a 2D image: who may, what is recomputed, and what stays uncalibrated.

The rule the whole photo surface rests on is *no ``pixelSpacing`` unless it is actually
known*. Cornerstone then reports lengths in ``px`` and marks them uncalibrated, which is
the honest answer for a photograph; fabricating 1 mm/px would report a fiction in
millimetres that nothing downstream could tell from a real measurement.

So the cases worth pinning are the refusals, and one in particular: **the server derives
the scale from the two points and ignores anything the client calculated.** That single
number rescales the millimetre reading of every length ever taken on the image, and a
value the server cannot re-derive is a value nobody can check.
"""

import json
import uuid

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from common.imaging_calibration import (
    CalibrationError,
    calibration_record,
    pixel_spacing_mm,
    spacing_from_known_length,
)
from common.models import FileRegistry, Project, ProjectAccess
from maxillo.models import Folder, Patient


class SpacingMathTests(SimpleTestCase):
    """Pure, so these are literals with answers known by hand."""

    def test_ten_millimetres_over_a_hundred_pixels_is_a_tenth(self):
        spacing, distance = spacing_from_known_length([0, 0], [100, 0], 10)
        self.assertAlmostEqual(spacing, 0.1, places=12)
        self.assertAlmostEqual(distance, 100.0, places=12)

    def test_the_distance_is_euclidean_not_axis_aligned(self):
        spacing, distance = spacing_from_known_length([0, 0], [30, 40], 10)
        self.assertAlmostEqual(distance, 50.0, places=12)
        self.assertAlmostEqual(spacing, 0.2, places=12)

    def test_a_sub_pixel_line_is_refused_rather_than_producing_a_huge_scale(self):
        """Not merely "not zero".

        A line a fraction of a pixel long divides a real length by almost nothing, and
        the resulting scale is enormous, confident, and entirely a function of where two
        clicks landed.
        """
        with self.assertRaisesMessage(CalibrationError, "less than one pixel"):
            spacing_from_known_length([10, 10], [10.4, 10], 10)

    def test_a_non_positive_length_is_refused(self):
        for bad in (0, -5):
            with self.assertRaises(CalibrationError):
                spacing_from_known_length([0, 0], [100, 0], bad)

    def test_non_finite_and_malformed_points_are_refused(self):
        with self.assertRaises(CalibrationError):
            spacing_from_known_length([0, float("nan")], [100, 0], 10)
        with self.assertRaises(CalibrationError):
            spacing_from_known_length([0, 0, 0], [100, 0], 10)
        with self.assertRaises(CalibrationError):
            spacing_from_known_length([0, 0], [100, 0], float("inf"))

    def test_a_boolean_is_not_a_number(self):
        """`True` is an int in Python and would otherwise pass every numeric check."""
        with self.assertRaises(CalibrationError):
            spacing_from_known_length([0, 0], [100, 0], True)

    def test_the_record_carries_its_provenance(self):
        record = calibration_record(
            0.1, known_length_mm=10, pixel_distance=100.0, user=None, now=timezone.now()
        )
        self.assertEqual(record["x_mm"], 0.1)
        self.assertEqual(
            record["y_mm"], record["x_mm"],
            "two points give one scalar; the two axes were not measured independently",
        )
        self.assertEqual(record["source"], "known_length")
        self.assertIn("calibrated_at", record)


class PixelSpacingReadTests(SimpleTestCase):
    """What a viewer is told, and the one answer that must never be defaulted."""

    class _File:
        def __init__(self, metadata):
            self.metadata = metadata

    def test_an_uncalibrated_file_reads_as_none_not_as_one(self):
        for metadata in ({}, None, {"pixel_spacing_mm": None}, {"other": 1}):
            self.assertIsNone(
                pixel_spacing_mm(self._File(metadata)),
                "a default of 1.0 would report a fiction in millimetres",
            )

    def test_a_bare_number_is_accepted_as_an_isotropic_spacing(self):
        """So a value written by hand or by a future importer never crashes a viewer."""
        self.assertEqual(pixel_spacing_mm(self._File({"pixel_spacing_mm": 0.2})), (0.2, 0.2))

    def test_the_full_record_reads_back_as_its_two_axes(self):
        self.assertEqual(
            pixel_spacing_mm(self._File({"pixel_spacing_mm": {"x_mm": 0.1, "y_mm": 0.3}})),
            (0.1, 0.3),
        )

    def test_a_nonsensical_stored_value_reads_as_uncalibrated(self):
        for stored in (0, -1, "0.1", True, {"x_mm": 0.1}, {"x_mm": 0.1, "y_mm": 0}):
            self.assertIsNone(
                pixel_spacing_mm(self._File({"pixel_spacing_mm": stored})),
                f"{stored!r} must not become a scale",
            )


class CalibrationEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        suffix = uuid.uuid4().hex[:8]
        cls.project = Project.objects.create(
            name=f"cal-{suffix}", slug=f"cal-{suffix}", domain="maxillo"
        )
        cls.folder = Folder.objects.create(name="mx", project=cls.project)
        cls.patient = Patient.objects.create(
            patient_id=9501, folder=cls.folder, project=cls.project
        )
        cls.other_patient = Patient.objects.create(
            patient_id=9502, folder=cls.folder, project=cls.project
        )
        cls.image = cls._file(cls.patient, "a.jpg", "a")
        cls.foreign = cls._file(cls.other_patient, "x.jpg", "x")

    @classmethod
    def _file(cls, patient, name, hash_char):
        return FileRegistry.objects.create(
            patient=patient,
            file_type="teleradiography_processed",
            file_path=f"maxillo/teleradiography_processed/{name}",
            file_size=1,
            file_hash=hash_char * 64,
            domain="maxillo",
        )

    def setUp(self):
        self.user = self._user("annotator")
        self.client.force_login(self.user)

    def _user(self, role):
        user = User.objects.create_user(
            username=f"u{uuid.uuid4().hex[:8]}", password="pw"  # noqa: S106
        )
        if role:
            ProjectAccess.objects.create(user=user, project=self.project, role=role)
        return user

    def _url(self, file_obj=None):
        return reverse(
            "maxillo:patient_image_calibration",
            kwargs={
                "patient_id": self.patient.patient_id,
                "file_id": (file_obj or self.image).id,
            },
        )

    def _post(self, body, file_obj=None):
        return self.client.post(
            self._url(file_obj), data=json.dumps(body), content_type="application/json"
        )

    def _calibrate(self, **extra):
        return self._post(
            {"pointA": [0, 0], "pointB": [100, 0], "knownLengthMm": 10.0, **extra}
        )

    def test_a_calibration_is_written_with_its_provenance(self):
        response = self._calibrate()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertAlmostEqual(body["pixelSpacingMm"]["x_mm"], 0.1, places=12)
        self.assertEqual(body["pixelSpacingMm"]["calibrated_by"], self.user.username)
        self.assertFalse(body["recalibrated"])

        self.image.refresh_from_db()
        self.assertEqual(pixel_spacing_mm(self.image), (0.1, 0.1))

    def test_the_server_ignores_a_client_supplied_scale(self):
        """The point of the endpoint.

        A client that computed its own number -- or was made to send a wrong one -- must
        not be able to set the scale every millimetre reading on the image depends on.
        """
        response = self._calibrate(mmPerPixel=99.0, pixelSpacingMm={"x_mm": 99.0})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertAlmostEqual(response.json()["pixelSpacingMm"]["x_mm"], 0.1, places=12)

    def test_recalibrating_keeps_the_previous_value_and_reports_the_change(self):
        """Allowed, not refused.

        The usual reason to recalibrate is that the first attempt was wrong, and refusing
        would leave deleting the work as the only fix.
        """
        self._calibrate()
        response = self._post(
            {"pointA": [0, 0], "pointB": [50, 0], "knownLengthMm": 10.0}
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["recalibrated"])
        self.assertIn("affectedMeasurements", response.json())

        self.image.refresh_from_db()
        self.assertAlmostEqual(pixel_spacing_mm(self.image)[0], 0.2, places=12)
        history = self.image.metadata["pixel_spacing_mm_history"]
        self.assertEqual(len(history), 1)
        self.assertAlmostEqual(history[0]["x_mm"], 0.1, places=12)

    def test_calibration_does_not_disturb_other_metadata(self):
        self.image.metadata = {"image_width": 800, "image_height": 600}
        self.image.save(update_fields=["metadata"])
        self._calibrate()
        self.image.refresh_from_db()
        self.assertEqual(self.image.metadata["image_width"], 800)
        self.assertEqual(self.image.metadata["image_height"], 600)

    def test_a_bad_measurement_is_a_400_and_writes_nothing(self):
        for body in (
            {"pointA": [0, 0], "pointB": [0, 0], "knownLengthMm": 10.0},
            {"pointA": [0, 0], "pointB": [100, 0], "knownLengthMm": 0},
            {"pointA": [0, 0], "pointB": [100, 0]},
            {"pointB": [100, 0], "knownLengthMm": 10.0},
        ):
            response = self._post(body)
            self.assertEqual(response.status_code, 400, body)
        self.image.refresh_from_db()
        self.assertIsNone(pixel_spacing_mm(self.image))

    def test_a_malformed_body_is_a_400(self):
        response = self.client.post(
            self._url(), data="not json", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_another_patients_file_is_a_403(self):
        response = self._post(
            {"pointA": [0, 0], "pointB": [100, 0], "knownLengthMm": 10.0},
            file_obj=self.foreign,
        )
        self.assertEqual(response.status_code, 403)
        self.foreign.refresh_from_db()
        self.assertIsNone(pixel_spacing_mm(self.foreign))

    def test_a_reader_may_not_calibrate(self):
        self.client.force_login(self._user("viewer"))
        self.assertEqual(self._calibrate().status_code, 403)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self._url()).status_code, 405)

    def test_anonymous_is_redirected_to_login(self):
        self.client.logout()
        self.assertIn(self._calibrate().status_code, (302, 403))
