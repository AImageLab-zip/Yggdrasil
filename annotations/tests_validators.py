"""The pure validation rules. No database, so ``SimpleTestCase``.

Each case is a way to write a row that reads back as valid and describes
something other than what its author meant. A polygon with two points, a
normalized coordinate of 1.4, a slice_pixel spline with no slice, a millimetre
length on an uncalibrated photograph: none of them raise anywhere downstream,
and all of them are wrong on a screen a clinician is looking at.
"""

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from annotations.constants import (
    CoordinateSystem,
    Geometry2DType,
    Geometry3DType,
    MeasurementKind,
    MeasurementUnit,
    SelectorKind,
    SliceAxis,
)
from annotations.validators import (
    validate_geometry_2d,
    validate_geometry_3d,
    validate_item_selector_pairing,
    validate_measurement,
    validate_selector,
)


class Geometry2DTests(SimpleTestCase):
    def _polygon(self, points, **kwargs):
        kwargs.setdefault("coordinate_system", CoordinateSystem.IMAGE_PIXEL)
        kwargs.setdefault("closed", True)
        return validate_geometry_2d(
            geometry_type=Geometry2DType.POLYGON, points=points, **kwargs
        )

    def test_a_polygon_needs_three_points(self):
        with self.assertRaises(ValidationError):
            self._polygon([[0, 0], [1, 1]])

    def test_a_polygon_must_be_closed(self):
        with self.assertRaises(ValidationError) as caught:
            self._polygon([[0, 0], [1, 0], [1, 1]], closed=False)

        self.assertIn("polyline", str(caught.exception))

    def test_ordinates_come_back_as_floats(self):
        """So a stored row does not depend on whether the client sent ints."""
        cleaned = self._polygon([[0, 0], [10, 0], [10, 10]])

        self.assertEqual(cleaned, [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]])
        self.assertIsInstance(cleaned[0][0], float)

    def test_a_three_ordinate_point_is_refused(self):
        with self.assertRaises(ValidationError):
            self._polygon([[0, 0, 0], [1, 0, 0], [1, 1, 0]])

    def test_nan_and_infinity_are_not_coordinates(self):
        for bad in (float("nan"), float("inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self._polygon([[0, 0], [1, 0], [bad, 1]])

    def test_a_three_space_frame_cannot_hold_a_planar_shape(self):
        with self.assertRaises(ValidationError):
            self._polygon(
                [[0, 0], [1, 0], [1, 1]],
                coordinate_system=CoordinateSystem.PATIENT_LPS_MM,
            )

    def test_a_normalized_ordinate_outside_the_unit_interval_is_refused(self):
        """A fraction above 1 is a pixel index that lost its frame."""
        with self.assertRaises(ValidationError) as caught:
            validate_geometry_2d(
                geometry_type=Geometry2DType.POINT,
                coordinate_system=CoordinateSystem.VIDEO_NORMALIZED,
                points=[[0.5, 640]],
            )

        self.assertIn("[0, 1]", str(caught.exception))

    def test_the_same_value_is_fine_as_a_pixel_index(self):
        cleaned = validate_geometry_2d(
            geometry_type=Geometry2DType.POINT,
            coordinate_system=CoordinateSystem.VIDEO_PIXEL,
            points=[[0.5, 640]],
        )

        self.assertEqual(cleaned, [[0.5, 640.0]])

    def test_fixed_arity_shapes_are_exact(self):
        for geometry_type in (
            Geometry2DType.RECTANGLE,
            Geometry2DType.ELLIPSE,
            Geometry2DType.CIRCLE,
        ):
            with self.subTest(geometry_type=geometry_type):
                validate_geometry_2d(
                    geometry_type=geometry_type,
                    coordinate_system=CoordinateSystem.IMAGE_PIXEL,
                    points=[[0, 0], [10, 10]],
                )
                with self.assertRaises(ValidationError):
                    validate_geometry_2d(
                        geometry_type=geometry_type,
                        coordinate_system=CoordinateSystem.IMAGE_PIXEL,
                        points=[[0, 0], [10, 10], [20, 20]],
                    )


class Geometry3DTests(SimpleTestCase):
    def test_a_planar_frame_cannot_hold_a_three_space_shape(self):
        with self.assertRaises(ValidationError):
            validate_geometry_3d(
                geometry_type=Geometry3DType.POINT,
                coordinate_system=CoordinateSystem.IMAGE_PIXEL,
                points=[[1, 2, 3]],
            )

    def test_a_landmark_is_three_ordinates(self):
        with self.assertRaises(ValidationError):
            validate_geometry_3d(
                geometry_type=Geometry3DType.POINT,
                coordinate_system=CoordinateSystem.RESOURCE_LOCAL,
                points=[[1, 2]],
            )

    def test_a_resource_scoped_frame_cannot_claim_a_frame_of_reference(self):
        """It would assert cross-series comparability that does not exist."""
        for coordinate_system in (
            CoordinateSystem.RESOURCE_LOCAL,
            CoordinateSystem.VOLUME_VOXEL,
        ):
            with self.subTest(coordinate_system=coordinate_system):
                with self.assertRaises(ValidationError):
                    validate_geometry_3d(
                        geometry_type=Geometry3DType.POINT,
                        coordinate_system=coordinate_system,
                        points=[[1, 2, 3]],
                        frame_of_reference_uid="1.2.840.10008.1.2",
                    )

    def test_a_patient_frame_may_carry_one(self):
        cleaned = validate_geometry_3d(
            geometry_type=Geometry3DType.POINT,
            coordinate_system=CoordinateSystem.PATIENT_LPS_MM,
            points=[[1, 2, 3]],
            frame_of_reference_uid="1.2.840.10008.1.2",
        )

        self.assertEqual(cleaned, [[1.0, 2.0, 3.0]])

    def test_a_sphere_without_a_radius_is_not_a_sphere(self):
        with self.assertRaises(ValidationError):
            validate_geometry_3d(
                geometry_type=Geometry3DType.SPHERE,
                coordinate_system=CoordinateSystem.PATIENT_LPS_MM,
                points=[[0, 0, 0]],
                attributes={},
            )

    def test_a_sphere_radius_must_be_positive(self):
        with self.assertRaises(ValidationError):
            validate_geometry_3d(
                geometry_type=Geometry3DType.SPHERE,
                coordinate_system=CoordinateSystem.PATIENT_LPS_MM,
                points=[[0, 0, 0]],
                attributes={"radius": 0},
            )


class SelectorTests(SimpleTestCase):
    def test_a_slice_selector_needs_both_axis_and_index(self):
        with self.assertRaises(ValidationError):
            validate_selector(
                kind=SelectorKind.SLICE,
                coordinate_system=CoordinateSystem.SLICE_PIXEL,
                slice_index=128,
            )

    def test_a_frame_selector_carrying_a_slice_index_is_refused_not_ignored(self):
        """Ignoring it is how the wrong field becomes the one that gets read."""
        with self.assertRaises(ValidationError) as caught:
            validate_selector(
                kind=SelectorKind.FRAME,
                coordinate_system=CoordinateSystem.VIDEO_PIXEL,
                frame_index=10,
                slice_index=5,
            )

        self.assertIn("slice_index", str(caught.exception))

    def test_a_whole_resource_selector_addresses_nothing_else(self):
        validate_selector(
            kind=SelectorKind.WHOLE_RESOURCE, coordinate_system=CoordinateSystem.NONE
        )
        with self.assertRaises(ValidationError):
            validate_selector(
                kind=SelectorKind.WHOLE_RESOURCE,
                coordinate_system=CoordinateSystem.NONE,
                frame_index=0,
            )

    def test_a_reversed_interval_is_refused(self):
        with self.assertRaises(ValidationError):
            validate_selector(
                kind=SelectorKind.TEMPORAL_INTERVAL,
                coordinate_system=CoordinateSystem.VIDEO_PIXEL,
                start_time_ms=5000,
                end_time_ms=4000,
            )

    def test_times_must_be_integers(self):
        with self.assertRaises(ValidationError):
            validate_selector(
                kind=SelectorKind.TEMPORAL_INTERVAL,
                coordinate_system=CoordinateSystem.VIDEO_PIXEL,
                start_time_ms=5.5,
                end_time_ms=6000,
            )

    def test_bounds_arity_follows_the_coordinate_system(self):
        validate_selector(
            kind=SelectorKind.SPATIAL_BOUNDS,
            coordinate_system=CoordinateSystem.IMAGE_PIXEL,
            bounds={"min": [0, 0], "max": [10, 10]},
        )
        with self.assertRaises(ValidationError):
            validate_selector(
                kind=SelectorKind.SPATIAL_BOUNDS,
                coordinate_system=CoordinateSystem.PATIENT_LPS_MM,
                bounds={"min": [0, 0], "max": [10, 10]},
            )

    def test_an_inverted_bounds_box_is_refused(self):
        with self.assertRaises(ValidationError):
            validate_selector(
                kind=SelectorKind.SPATIAL_BOUNDS,
                coordinate_system=CoordinateSystem.IMAGE_PIXEL,
                bounds={"min": [0, 10], "max": [10, 0]},
            )


class ItemSelectorPairingTests(SimpleTestCase):
    def test_slice_pixel_geometry_without_a_slice_selector_is_unplaceable(self):
        """The panoramic arch is exactly this shape: a spline in one axial slice."""
        with self.assertRaises(ValidationError):
            validate_item_selector_pairing(
                coordinate_system=CoordinateSystem.SLICE_PIXEL, selector_kind=None
            )
        with self.assertRaises(ValidationError):
            validate_item_selector_pairing(
                coordinate_system=CoordinateSystem.SLICE_PIXEL,
                selector_kind=SelectorKind.FRAME,
            )

    def test_slice_pixel_with_a_slice_selector_is_fine(self):
        validate_item_selector_pairing(
            coordinate_system=CoordinateSystem.SLICE_PIXEL,
            selector_kind=SelectorKind.SLICE,
            selector_axis=SliceAxis.AXIAL,
        )

    def test_video_geometry_needs_a_frame_or_interval(self):
        with self.assertRaises(ValidationError):
            validate_item_selector_pairing(
                coordinate_system=CoordinateSystem.VIDEO_PIXEL,
                selector_kind=SelectorKind.SLICE,
                selector_axis=SliceAxis.AXIAL,
            )

    def test_an_image_annotation_needs_no_selector(self):
        validate_item_selector_pairing(
            coordinate_system=CoordinateSystem.IMAGE_PIXEL, selector_kind=None
        )


class MeasurementTests(SimpleTestCase):
    def test_millimetres_require_calibration(self):
        with self.assertRaises(ValidationError) as caught:
            validate_measurement(
                kind=MeasurementKind.LENGTH,
                value=12.5,
                unit=MeasurementUnit.MM,
                is_calibrated=False,
            )

        self.assertIn("pixels", str(caught.exception))

    def test_pixels_do_not(self):
        self.assertEqual(
            validate_measurement(
                kind=MeasurementKind.LENGTH,
                value=50,
                unit=MeasurementUnit.PX,
                is_calibrated=False,
            ),
            50.0,
        )

    def test_an_area_is_not_reported_in_a_length_unit(self):
        with self.assertRaises(ValidationError) as caught:
            validate_measurement(
                kind=MeasurementKind.AREA,
                value=10,
                unit=MeasurementUnit.MM,
                is_calibrated=True,
            )

        self.assertIn("mm2", str(caught.exception))

    def test_a_negative_length_is_refused_but_a_negative_hu_is_not(self):
        with self.assertRaises(ValidationError):
            validate_measurement(
                kind=MeasurementKind.LENGTH,
                value=-1,
                unit=MeasurementUnit.PX,
                is_calibrated=False,
            )

        self.assertEqual(
            validate_measurement(
                kind=MeasurementKind.MEAN,
                value=-412.0,
                unit=MeasurementUnit.HU,
                is_calibrated=True,
                sample_count=8192,
            ),
            -412.0,
        )

    def test_a_statistic_must_say_how_many_samples_it_covers(self):
        with self.assertRaises(ValidationError):
            validate_measurement(
                kind=MeasurementKind.MEAN,
                value=100.0,
                unit=MeasurementUnit.HU,
                is_calibrated=True,
            )

    def test_a_geometric_measure_has_no_sample_count(self):
        with self.assertRaises(ValidationError):
            validate_measurement(
                kind=MeasurementKind.LENGTH,
                value=10,
                unit=MeasurementUnit.PX,
                is_calibrated=False,
                sample_count=10,
            )
