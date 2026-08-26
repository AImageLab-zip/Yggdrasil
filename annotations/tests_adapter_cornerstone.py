"""Cornerstone tool state translates into descriptors, and its numbers are recomputed.

The adapter is pure, so these are plain unit tests with literal dicts -- no database,
no fixtures. What they pin is mostly *refusals*: the value the store keeps is not the
value the viewer reported, runtime identifiers do not survive, and an intensity reading
is not accepted at all.

Handle conventions are asserted against the ones read off ``@cornerstonejs/tools@5.8.2``
while writing the adapter. If a version bump changes one, these fail with a wrong
number rather than a wrong shape, which is the failure worth having.
"""

import math

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from annotations.adapters import cornerstone as cs
from annotations.adapters import descriptors as d
from annotations.constants import (
    CoordinateSystem,
    Geometry3DType,
    MeasurementKind,
    MeasurementUnit,
)


def annotation(tool_name, points, **extra):
    """One Cornerstone annotation, in the shape the tools actually produce."""
    return {
        "annotationUID": "0d3d1a3e-runtime-only",
        "metadata": {
            "toolName": tool_name,
            "FrameOfReferenceUID": "1.2.840.113619.2.55.3.12345",
            "referencedImageId": "nifti:https://host/v.nii.gz?frame=12",
            "viewPlaneNormal": [0, 0, 1],
            **extra.pop("metadata", {}),
        },
        "data": {
            "handles": {"points": points, "textBox": {"hasMoved": False}},
            "cachedStats": {"volumeId:nifti:https://host/v.nii.gz": {"length": 999.0}},
            "label": "",
        },
        **extra,
    }


def by_item(descriptors, item):
    return [entry for entry in descriptors if entry["item"] == item]


class GeometryMathTests(SimpleTestCase):
    """The recomputation itself, on shapes whose answer is known by hand."""

    def test_polyline_length_sums_its_segments(self):
        self.assertEqual(cs.polyline_length([[0, 0, 0], [3, 4, 0]]), 5.0)
        self.assertEqual(cs.polyline_length([[0, 0, 0], [3, 4, 0], [3, 4, 12]]), 17.0)

    def test_the_angle_vertex_is_the_middle_handle(self):
        """``AngleTool.js:411-418`` uses ``angleBetweenLines([p0,p1],[p1,p2])``.

        Taking the first or last handle as the vertex yields the supplement, which
        looks like a plausible angle and is wrong by however much the true one is not
        90 degrees -- so a right angle would hide the bug and 30 degrees exposes it.
        """
        thirty = cs.angle_at_vertex([[1, 0, 0], [0, 0, 0], [math.cos(math.radians(30)), math.sin(math.radians(30)), 0]])
        self.assertAlmostEqual(thirty, 30.0, places=9)

        right = cs.angle_at_vertex([[1, 0, 0], [0, 0, 0], [0, 1, 0]])
        self.assertAlmostEqual(right, 90.0, places=9)

    def test_a_degenerate_angle_is_refused(self):
        with self.assertRaises(ValidationError):
            cs.angle_at_vertex([[0, 0, 0], [0, 0, 0], [1, 0, 0]])

    def test_the_cobb_angle_is_undirected(self):
        """Reversing either handle pair must not change the answer."""
        lines = [[0, 0, 0], [1, 0, 0], [0, 0, 0], [1, 1, 0]]
        expected = cs.angle_between_lines(lines)
        self.assertAlmostEqual(expected, 45.0, places=9)

        reversed_first = [[1, 0, 0], [0, 0, 0], [0, 0, 0], [1, 1, 0]]
        self.assertAlmostEqual(cs.angle_between_lines(reversed_first), expected, places=9)

        reversed_second = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 0, 0]]
        self.assertAlmostEqual(cs.angle_between_lines(reversed_second), expected, places=9)

    def test_polygon_area_works_on_an_oblique_plane(self):
        """A 2D shoelace on projected coordinates would under-report a tilted ROI.

        An oblique reformat's rectangle is not axis-aligned in patient space, so the
        area has to be computed in three dimensions.
        """
        # A 3x4 rectangle lying in the plane z = x, i.e. tilted 45 degrees.
        tilted = [[0, 0, 0], [3, 0, 3], [3, 4, 3], [0, 4, 0]]
        self.assertAlmostEqual(cs.polygon_area(tilted), math.hypot(3, 3) * 4, places=9)

        flat = [[0, 0, 0], [3, 0, 0], [3, 4, 0], [0, 4, 0]]
        self.assertAlmostEqual(cs.polygon_area(flat), 12.0, places=9)

    def test_polygon_area_needs_three_points(self):
        with self.assertRaises(ValidationError):
            cs.polygon_area([[0, 0, 0], [1, 0, 0]])

    def test_circle_radius_is_centre_to_circumference(self):
        # CircleROITool.js:181-189: points[0] is the centre, points[1] on the edge.
        self.assertEqual(cs.circle_radius([[0, 0, 0], [0, 5, 0]]), 5.0)

    def test_ellipse_semi_axes_are_half_each_handle_pair(self):
        # EllipticalROITool.js:275-312 moves 0/1 as one axis and 2/3 as the other.
        major, minor = cs.ellipse_semi_axes([[0, -4, 0], [0, 4, 0], [-2, 0, 0], [2, 0, 0]])
        self.assertEqual((major, minor), (4.0, 2.0))


class RuntimeIdentifierTests(SimpleTestCase):
    """Session-scoped ids do not reach the store; the DICOM one does."""

    def test_every_runtime_key_is_stripped_at_any_depth(self):
        # A walk, not a top-level key test: the realistic way one gets in is nested
        # inside a tool's own data blob.
        payload = {
            "annotationUID": "x",
            "data": {
                "cachedStats": {"volumeId:abc": {"mean": 1}},
                "nested": [{"referencedImageId": "y", "keep": 1}],
            },
            "keep": "yes",
        }
        cleaned = cs.strip_runtime_identifiers(payload)
        self.assertEqual(cleaned, {"data": {"nested": [{"keep": 1}]}, "keep": "yes"})

    def test_the_original_payload_is_not_mutated(self):
        payload = {"annotationUID": "x", "keep": 1}
        cs.strip_runtime_identifiers(payload)
        self.assertIn("annotationUID", payload)

    def test_the_frame_of_reference_uid_is_kept(self):
        """It is DICOM's identifier, not Cornerstone's, and has a column for it."""
        descriptors = cs.descriptors_for_annotation(
            annotation("Length", [[0, 0, 0], [3, 4, 0]])
        )
        geometry = by_item(descriptors, d.SPATIAL_3D)[0]
        self.assertEqual(geometry["frame_of_reference_uid"], "1.2.840.113619.2.55.3.12345")

    def test_the_resumable_payload_carries_no_runtime_identifiers(self):
        payload = cs.cornerstone_state_payload([annotation("Length", [[0, 0, 0], [1, 0, 0]])])
        text = repr(payload)
        for key in ("annotationUID", "cachedStats", "referencedImageId"):
            self.assertNotIn(key, text)
        # ...but it is still a usable scratch copy: the handles survive.
        self.assertEqual(
            payload["annotations"][0]["data"]["handles"]["points"], [[0, 0, 0], [1, 0, 0]]
        )


class ToolTranslationTests(SimpleTestCase):
    """One case per tool, with the number checked against hand arithmetic."""

    def test_length_is_recomputed_and_the_viewer_number_ignored(self):
        """``cachedStats`` says 999; the geometry says 5. The geometry wins.

        This is the whole point of the adapter. `cachedStats` is Cornerstone's own
        cache and is stale between edits by design, so a store that trusted it would
        record a number that disagrees with the shape stored beside it.
        """
        descriptors = cs.descriptors_for_annotation(
            annotation("Length", [[0, 0, 0], [3, 4, 0]])
        )
        measurement = by_item(descriptors, d.MEASUREMENT)[0]
        self.assertEqual(measurement["value"], 5.0)
        self.assertNotEqual(measurement["value"], 999.0)
        self.assertEqual(measurement["kind"], MeasurementKind.LENGTH)
        self.assertEqual(measurement["unit"], MeasurementUnit.MM)
        self.assertTrue(measurement["is_calibrated"])

        geometry = by_item(descriptors, d.SPATIAL_3D)[0]
        self.assertEqual(geometry["geometry_type"], Geometry3DType.POLYLINE)
        self.assertEqual(geometry["coordinate_system"], CoordinateSystem.PATIENT_LPS_MM)

    def test_height_measures_length_like_length_does(self):
        descriptors = cs.descriptors_for_annotation(annotation("Height", [[0, 0, 0], [0, 0, 7]]))
        self.assertEqual(by_item(descriptors, d.MEASUREMENT)[0]["value"], 7.0)

    def test_angle_reports_degrees_and_is_calibrated_without_a_scale(self):
        """An angle is dimensionless, so it needs no millimetre scale to be true."""
        descriptors = cs.descriptors_for_annotation(
            annotation("Angle", [[1, 0, 0], [0, 0, 0], [0, 1, 0]])
        )
        measurement = by_item(descriptors, d.MEASUREMENT)[0]
        self.assertAlmostEqual(measurement["value"], 90.0, places=9)
        self.assertEqual(measurement["unit"], MeasurementUnit.DEG)
        self.assertTrue(measurement["is_calibrated"])

    def test_cobb_angle_uses_two_lines_not_a_shared_vertex(self):
        descriptors = cs.descriptors_for_annotation(
            annotation("CobbAngle", [[0, 0, 0], [1, 0, 0], [5, 5, 0], [6, 6, 0]])
        )
        measurement = by_item(descriptors, d.MEASUREMENT)[0]
        self.assertAlmostEqual(measurement["value"], 45.0, places=9)

    def test_bidirectional_reports_two_axes_with_distinct_roles(self):
        """Summing the four handles as one polyline would report a meaningless total."""
        descriptors = cs.descriptors_for_annotation(
            annotation("Bidirectional", [[0, 0, 0], [10, 0, 0], [5, -2, 0], [5, 2, 0]])
        )
        measurements = by_item(descriptors, d.MEASUREMENT)
        self.assertEqual(len(measurements), 2)
        self.assertEqual(
            {entry["role"]: entry["value"] for entry in measurements},
            {"long_axis": 10.0, "short_axis": 4.0},
        )

    def test_rectangle_area_survives_cornerstone_corner_ordering(self):
        """The bow-tie case, and it fails to *zero* rather than to a wrong number.

        `RectangleROITool.js:215-239` stores corners as
        (bottomLeft, bottomRight, topLeft, topRight), so a shoelace walked in index
        order traces a self-intersecting figure whose signed area cancels exactly. A
        4x3 rectangle would be recorded as 0 mm2, and nothing about the shape would
        look wrong.
        """
        corners = [[0, 0, 0], [4, 0, 0], [0, 3, 0], [4, 3, 0]]  # BL, BR, TL, TR
        descriptors = cs.descriptors_for_annotation(annotation("RectangleROI", corners))

        area = next(e for e in by_item(descriptors, d.MEASUREMENT) if e["kind"] == MeasurementKind.AREA)
        self.assertAlmostEqual(area["value"], 12.0, places=9)
        self.assertEqual(area["unit"], MeasurementUnit.MM2)

        perimeter = next(
            e for e in by_item(descriptors, d.MEASUREMENT) if e["kind"] == MeasurementKind.PERIMETER
        )
        self.assertAlmostEqual(perimeter["value"], 14.0, places=9)

        # Walking the handles in the order Cornerstone gave them is the bug:
        self.assertAlmostEqual(cs.polygon_area(corners), 0.0, places=9)

        # The stored geometry keeps Cornerstone's own order, so a round trip back into
        # the viewer puts the handles in the right corners.
        self.assertEqual(by_item(descriptors, d.SPATIAL_3D)[0]["points"], corners)

    def test_circle_stores_a_sphere_with_its_radius_and_reports_both_numbers(self):
        descriptors = cs.descriptors_for_annotation(
            annotation("CircleROI", [[1, 2, 3], [1, 7, 3]])
        )
        geometry = by_item(descriptors, d.SPATIAL_3D)[0]
        self.assertEqual(geometry["geometry_type"], Geometry3DType.SPHERE)
        self.assertEqual(geometry["points"], [[1.0, 2.0, 3.0]], "a sphere stores its centre")
        self.assertEqual(geometry["attributes"]["radius"], 5.0)

        measurements = {entry["kind"]: entry["value"] for entry in by_item(descriptors, d.MEASUREMENT)}
        self.assertAlmostEqual(measurements[MeasurementKind.DIAMETER], 10.0, places=9)
        self.assertAlmostEqual(measurements[MeasurementKind.AREA], math.pi * 25, places=9)

    def test_ellipse_area_uses_both_semi_axes(self):
        descriptors = cs.descriptors_for_annotation(
            annotation("EllipticalROI", [[0, -4, 0], [0, 4, 0], [-2, 0, 0], [2, 0, 0]])
        )
        area = by_item(descriptors, d.MEASUREMENT)[0]
        self.assertAlmostEqual(area["value"], math.pi * 4 * 2, places=9)

    def test_a_probe_keeps_its_point_and_refuses_the_intensity_reading(self):
        """A Hounsfield value needs the voxels; an adapter does not have them.

        Decision #11 puts ROI statistics in the first release -- computed server-side
        from the volume, not accepted from a client that can be asked to report
        anything.
        """
        descriptors = cs.descriptors_for_annotation(annotation("Probe", [[1, 2, 3]]))
        self.assertEqual(len(descriptors), 1)
        self.assertEqual(descriptors[0]["item"], d.SPATIAL_3D)
        self.assertEqual(descriptors[0]["geometry_type"], Geometry3DType.POINT)
        self.assertEqual(by_item(descriptors, d.MEASUREMENT), [])


class RefusalTests(SimpleTestCase):
    """What the adapter declines to translate, and why."""

    def test_an_unmapped_tool_is_an_error_not_a_bare_payload(self):
        with self.assertRaises(ValidationError) as caught:
            cs.descriptors_for_annotation(annotation("ArrowAnnotate", [[0, 0, 0]]))
        self.assertIn("no descriptor mapping", str(caught.exception))

    def test_an_incomplete_annotation_is_refused(self):
        """A two-handle angle is a half-drawn one, not a small one."""
        with self.assertRaises(ValidationError) as caught:
            cs.descriptors_for_annotation(annotation("Angle", [[0, 0, 0], [1, 0, 0]]))
        self.assertIn("exactly 3 handles", str(caught.exception))

    def test_a_non_finite_handle_is_refused(self):
        # NaN reaches the database as a perfectly storable null island.
        for bad in (float("nan"), float("inf")):
            with self.assertRaises(ValidationError):
                cs.descriptors_for_annotation(annotation("Length", [[0, 0, 0], [bad, 0, 0]]))

    def test_a_two_dimensional_handle_is_refused(self):
        with self.assertRaises(ValidationError):
            cs.descriptors_for_annotation(annotation("Length", [[0, 0], [1, 1]]))

    def test_an_annotation_with_no_tool_name_is_refused(self):
        payload = annotation("Length", [[0, 0, 0], [1, 0, 0]])
        payload["metadata"].pop("toolName")
        with self.assertRaises(ValidationError):
            cs.descriptors_for_annotation(payload)

    def test_an_annotation_with_no_handles_is_refused(self):
        payload = annotation("Length", [])
        with self.assertRaises(ValidationError):
            cs.descriptors_for_annotation(payload)

    def test_an_unknown_coordinate_system_is_refused(self):
        with self.assertRaises(ValidationError):
            cs.descriptors_for_annotation(
                annotation("Length", [[0, 0, 0], [1, 0, 0]]), coordinate_system="patient_world"
            )


class CalibrationTests(SimpleTestCase):
    """Millimetres are claimed only where the frame earns them."""

    def test_a_millimetre_frame_yields_calibrated_millimetres(self):
        for frame in (CoordinateSystem.PATIENT_LPS_MM, CoordinateSystem.PATIENT_RAS_MM):
            descriptors = cs.descriptors_for_annotation(
                annotation("Length", [[0, 0, 0], [3, 4, 0]]), coordinate_system=frame
            )
            measurement = by_item(descriptors, d.MEASUREMENT)[0]
            self.assertEqual(measurement["unit"], MeasurementUnit.MM)
            self.assertTrue(measurement["is_calibrated"])
            self.assertEqual(measurement["calibration_note"], "")

    def test_a_non_millimetre_frame_yields_pixels_and_says_why(self):
        """The rule ``MeasurementItem``'s CHECK constraint enforces, applied earlier.

        A clinician reading "12.4 mm" off an uncalibrated frame gets a wrong number
        with no way to tell.
        """
        descriptors = cs.descriptors_for_annotation(
            annotation("Length", [[0, 0, 0], [3, 4, 0]]),
            coordinate_system=CoordinateSystem.VOLUME_VOXEL,
        )
        measurement = by_item(descriptors, d.MEASUREMENT)[0]
        self.assertEqual(measurement["unit"], MeasurementUnit.PX)
        self.assertFalse(measurement["is_calibrated"])
        self.assertIn("uncalibrated", measurement["calibration_note"])

    def test_an_uncalibrated_area_is_square_pixels(self):
        descriptors = cs.descriptors_for_annotation(
            annotation("RectangleROI", [[0, 0, 0], [4, 0, 0], [0, 3, 0], [4, 3, 0]]),
            coordinate_system=CoordinateSystem.VOLUME_VOXEL,
        )
        area = next(e for e in by_item(descriptors, d.MEASUREMENT) if e["kind"] == MeasurementKind.AREA)
        self.assertEqual(area["unit"], MeasurementUnit.PX2)
        self.assertFalse(area["is_calibrated"])

    def test_an_angle_stays_calibrated_in_an_uncalibrated_frame(self):
        """Degrees are dimensionless: a voxel-frame angle is still a true angle."""
        descriptors = cs.descriptors_for_annotation(
            annotation("Angle", [[1, 0, 0], [0, 0, 0], [0, 1, 0]]),
            coordinate_system=CoordinateSystem.VOLUME_VOXEL,
        )
        measurement = by_item(descriptors, d.MEASUREMENT)[0]
        self.assertEqual(measurement["unit"], MeasurementUnit.DEG)
        self.assertTrue(measurement["is_calibrated"])


class GroupingTests(SimpleTestCase):
    """Geometry and its measurements travel together."""

    def test_all_descriptors_share_the_order_selector_and_label(self):
        selector = d.slice_selector(
            axis="axial", index=42, coordinate_system=CoordinateSystem.PATIENT_LPS_MM
        )
        descriptors = cs.descriptors_for_annotation(
            annotation("Bidirectional", [[0, 0, 0], [10, 0, 0], [5, -2, 0], [5, 2, 0]]),
            selector=selector,
            label_code="lesion",
            order=7,
        )
        self.assertEqual(len(descriptors), 3)
        for entry in descriptors:
            self.assertEqual(entry["order"], 7)
            self.assertEqual(entry["label_code"], "lesion")
            self.assertEqual(entry["selector"], selector)

    def test_the_geometry_descriptor_comes_first(self):
        """`apply_descriptors` attaches a measurement to the shape before it."""
        descriptors = cs.descriptors_for_annotation(
            annotation("CircleROI", [[0, 0, 0], [0, 1, 0]])
        )
        self.assertEqual(descriptors[0]["item"], d.SPATIAL_3D)
        self.assertTrue(all(entry["item"] == d.MEASUREMENT for entry in descriptors[1:]))
