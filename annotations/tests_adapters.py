"""Legacy -> annotation-model translation. Pure, so ``SimpleTestCase``.

Decision #6 keeps the legacy tables readable for one release as a cross-check,
and a cross-check only means something if the conversion is lossless. So most of
these assert that something survived: the prompt-point polarity SAM2 needs, the
distinction between a brush stroke and an eraser stroke, the classifier that
produced a classification, the third plane axis that a cross product would only
approximately reconstruct.

The rest assert the two conversions that are genuinely decisions rather than
copies: float seconds becoming integer milliseconds, and IOS landmarks landing
in ``resource_local`` rather than a patient frame.
"""

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from annotations.adapters import legacy_common, legacy_maxillo
from annotations.constants import CoordinateSystem, Geometry2DType, Geometry3DType


class IosLandmarkTests(SimpleTestCase):
    def _document(self, **entry):
        return {"7_upper_FDI_11": entry}

    def test_landmarks_are_resource_local_not_a_patient_frame(self):
        """They come from ``worldToLocal`` against a mesh with no registration."""
        out = legacy_maxillo.ios_landmarks(
            self._document(incisal=[1.0, 2.0, 3.0]), patient_id=7
        )

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["coordinate_system"], CoordinateSystem.RESOURCE_LOCAL)
        self.assertEqual(out[0]["points"], [[1.0, 2.0, 3.0]])

    def test_the_fdi_code_becomes_the_label_code(self):
        out = legacy_maxillo.ios_landmarks(
            self._document(incisal=[1, 2, 3]), patient_id=7
        )

        self.assertEqual(out[0]["label_code"], "11")
        self.assertEqual(out[0]["attributes"]["jaw"], "upper")

    def test_cusps_become_one_point_each_not_a_polyline(self):
        """They are unordered landmarks; a polyline would assert connectivity."""
        out = legacy_maxillo.ios_landmarks(
            self._document(cusps=[[1, 1, 1], [2, 2, 2], [3, 3, 3]]), patient_id=7
        )

        self.assertEqual(len(out), 3)
        for item in out:
            self.assertEqual(item["geometry_type"], Geometry3DType.POINT)
        self.assertEqual([item["order"] for item in out], [0, 1, 2])

    def test_the_base_plane_keeps_its_third_axis(self):
        """z is derivable, but not bit-identical to a recomputed cross product."""
        out = legacy_maxillo.ios_landmarks(
            self._document(
                basePlane={
                    "origin": [0, 0, 0],
                    "xAxis": [1, 0, 0],
                    "yAxis": [0, 1, 0],
                    "zAxis": [0, 0, 1],
                }
            ),
            patient_id=7,
        )

        self.assertEqual(out[0]["geometry_type"], Geometry3DType.PLANE)
        self.assertEqual(out[0]["points"], [[0, 0, 0], [1, 0, 0], [0, 1, 0]])
        self.assertEqual(out[0]["attributes"]["zAxis"], [0, 0, 1])

    def test_a_key_from_another_patient_is_refused(self):
        with self.assertRaises(ValidationError):
            legacy_maxillo.ios_landmarks(
                {"8_upper_FDI_11": {"incisal": [1, 2, 3]}}, patient_id=7
            )

    def test_a_malformed_key_is_refused(self):
        with self.assertRaises(ValidationError):
            legacy_maxillo.ios_landmarks({"tooth-eleven": {}}, patient_id=7)

    def test_an_incomplete_base_plane_is_refused(self):
        with self.assertRaises(ValidationError):
            legacy_maxillo.ios_landmarks(
                self._document(basePlane={"origin": [0, 0, 0], "xAxis": [1, 0, 0]}),
                patient_id=7,
            )

    def test_an_empty_document_converts_to_nothing_without_erroring(self):
        self.assertEqual(legacy_maxillo.ios_landmarks({}, patient_id=7), [])


class IntraoralSegmentationTests(SimpleTestCase):
    def test_each_polygon_of_a_tooth_becomes_its_own_item(self):
        out = legacy_maxillo.intraoral_segmentation(
            {"11": [[[0, 0], [1, 0], [1, 1]], [[5, 5], [6, 5], [6, 6]]]}
        )

        self.assertEqual(len(out), 2)
        self.assertEqual([item["order"] for item in out], [0, 1])
        self.assertEqual(out[0]["label_code"], "11")

    def test_polygons_are_closed_image_pixels(self):
        out = legacy_maxillo.intraoral_segmentation({"21": [[[0, 0], [1, 0], [1, 1]]]})

        self.assertEqual(out[0]["geometry_type"], Geometry2DType.POLYGON)
        self.assertTrue(out[0]["closed"])
        self.assertEqual(out[0]["coordinate_system"], CoordinateSystem.IMAGE_PIXEL)

    def test_coordinates_are_not_rescaled(self):
        """Normalizing here would make the converted form differ from the row."""
        out = legacy_maxillo.intraoral_segmentation(
            {"11": [[[0, 0], [1920, 0], [1920, 1080]]]}
        )

        self.assertEqual(out[0]["points"], [[0, 0], [1920, 0], [1920, 1080]])

    def test_teeth_are_converted_in_a_stable_order(self):
        out = legacy_maxillo.intraoral_segmentation(
            {"21": [[[0, 0], [1, 0], [1, 1]]], "11": [[[0, 0], [1, 0], [1, 1]]]}
        )

        self.assertEqual([item["label_code"] for item in out], ["11", "21"])


class PanoramicArchTests(SimpleTestCase):
    def _spline(self, points=None):
        return legacy_maxillo.panoramic_arch(
            points or [[0, 0], [10, 5], [20, 5], [30, 0]],
            axial_slice=128,
            volume_shape=[400, 400, 300],
            geometry_source="custom_cp",
            default_mode="mip",
            algorithm_version="panorex-js-v2-mip",
        )

    def test_the_arch_carries_its_slice_selector(self):
        """Without the axial index the curve cannot be placed in the volume."""
        out = self._spline()

        self.assertEqual(out[0]["coordinate_system"], CoordinateSystem.SLICE_PIXEL)
        self.assertEqual(out[0]["selector"]["kind"], "slice")
        self.assertEqual(out[0]["selector"]["slice_index"], 128)
        self.assertEqual(out[0]["selector"]["slice_axis"], "axial")

    def test_the_arch_is_an_open_polyline(self):
        out = self._spline()

        self.assertEqual(out[0]["geometry_type"], Geometry2DType.POLYLINE)
        self.assertFalse(out[0]["closed"])

    def test_the_generation_context_survives(self):
        out = self._spline()

        self.assertEqual(out[0]["attributes"]["geometry_source"], "custom_cp")
        self.assertEqual(out[0]["attributes"]["default_mode"], "mip")
        self.assertEqual(out[0]["attributes"]["volume_shape"], [400, 400, 300])

    def test_the_dict_form_of_the_column_is_accepted(self):
        """The save path accepts both shapes, so the conversion has to too."""
        out = legacy_maxillo.panoramic_arch(
            {"control_points": [[0, 0], [1, 1], [2, 2], [3, 3]]},
            axial_slice=1,
            volume_shape=[10, 10, 10],
            geometry_source="auto",
            default_mode="raysum",
        )

        self.assertEqual(len(out[0]["points"]), 4)

    def test_an_empty_arch_is_refused(self):
        with self.assertRaises(ValidationError):
            legacy_maxillo.panoramic_arch(
                [],
                axial_slice=1,
                volume_shape=[10, 10, 10],
                geometry_source="auto",
                default_mode="mip",
            )


class OcclusionClassificationTests(SimpleTestCase):
    def test_five_facets_become_five_events(self):
        out = legacy_maxillo.occlusion_classification(
            {
                "sagittal_left": "I",
                "sagittal_right": "II",
                "vertical": "Normal",
                "transverse": "Unknown",
                "midline": "Unknown",
            }
        )

        self.assertEqual(len(out), 5)
        self.assertEqual(out[0]["event_type"], "occlusion.sagittal_left")

    def test_unknown_is_carried_not_dropped(self):
        """Otherwise a reviewed-inconclusive case looks untouched."""
        out = legacy_maxillo.occlusion_classification({"vertical": "Unknown"})

        self.assertEqual(out[0]["value"], "Unknown")

    def test_the_classifier_survives(self):
        out = legacy_maxillo.occlusion_classification(
            {"vertical": "Normal"}, classifier="pipeline"
        )

        self.assertEqual(out[0]["attributes"]["classifier"], "pipeline")

    def test_a_row_with_no_facets_is_refused(self):
        with self.assertRaises(ValidationError):
            legacy_maxillo.occlusion_classification({})


class VoiceCaptionTests(SimpleTestCase):
    def test_the_transcript_becomes_the_value(self):
        out = legacy_common.voice_caption(transcript="Impacted third molar", duration=4.5)

        self.assertEqual(out[0]["event_type"], "voice_caption")
        self.assertEqual(out[0]["value"], "Impacted third molar")

    def test_a_pending_transcription_still_converts(self):
        """The recording is the work; the transcription is a machine step."""
        out = legacy_common.voice_caption(transcript="", duration=4.5, status="pending")

        self.assertEqual(out[0]["value"], "")
        self.assertFalse(out[0]["attributes"]["has_transcript"])

    def test_the_duration_stays_in_seconds(self):
        """It is a length, not a position; rounding it would be lossy."""
        out = legacy_common.voice_caption(transcript="x", duration=4.5)

        self.assertEqual(out[0]["attributes"]["duration_seconds"], 4.5)

    def test_a_negative_duration_is_refused(self):
        with self.assertRaises(ValidationError):
            legacy_common.voice_caption(transcript="x", duration=-1)
