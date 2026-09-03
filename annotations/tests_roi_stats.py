"""ROI statistics, on volumes small enough that the answer is known by hand.

The point of keeping ``annotations.roi_stats`` pure is that every one of these runs
against an 8x8x8 array whose contents were chosen so the expected mean can be written
down, instead of only against a real CBCT nobody can put in a test.

Two properties are worth more than the arithmetic:

* **Anisotropy.** A CBCT is 0.3 x 0.3 x 0.4 mm. A radius applied in index space selects
  an ellipsoid while reporting a sphere, and the error grows exactly where the spacing
  is least uniform -- so the sphere tests use anisotropic spacing on purpose.
* **The LPS/RAS flip.** Stored points are LPS, the affine maps to RAS. Getting it wrong
  mirrors the ROI across the sagittal and coronal planes and still returns a number.
"""

import numpy as np
from django.test import SimpleTestCase

from annotations import roi_stats
from annotations.constants import Geometry3DType


def affine_of(spacing, origin_ras=(0.0, 0.0, 0.0)):
    """A diagonal RAS affine: voxel indices to RAS millimetres."""
    matrix = np.eye(4)
    matrix[0, 0], matrix[1, 1], matrix[2, 2] = spacing
    matrix[:3, 3] = origin_ras
    return matrix


class CoordinateTests(SimpleTestCase):
    """The one sign flip, and the one matrix inversion."""

    def test_world_to_voxel_round_trips(self):
        affine = affine_of((0.3, 0.35, 0.4), origin_ras=(-10, 20, -5))
        for index in ([0, 0, 0], [7, 3, 5], [2.5, 1.25, 6.75]):
            world = roi_stats.voxel_to_world(affine, index)
            back = roi_stats.world_to_voxel(affine, world)
            np.testing.assert_allclose(back, index, atol=1e-9)

    def test_lps_and_ras_differ_by_two_sign_flips(self):
        np.testing.assert_allclose(roi_stats.lps_to_ras([1, 2, 3]), [-1, -2, 3])
        # ...and the flip is an involution, so applying it twice is identity.
        np.testing.assert_allclose(
            roi_stats.lps_to_ras(roi_stats.lps_to_ras([1, 2, 3])), [1, 2, 3]
        )

    def test_world_to_voxel_inverts_rather_than_transposes(self):
        """The two agree only for a rotation with unit spacing -- never a real CBCT."""
        affine = affine_of((0.3, 0.3, 0.4))
        # Voxel (10, 10, 10) is at RAS (3, 3, 4), i.e. LPS (-3, -3, 4).
        np.testing.assert_allclose(
            roi_stats.world_to_voxel(affine, [-3.0, -3.0, 4.0]), [10, 10, 10], atol=1e-9
        )
        # A transpose would have divided by nothing and landed at (3, 3, 4)*spacing.
        self.assertFalse(
            np.allclose(roi_stats.world_to_voxel(affine, [-3.0, -3.0, 4.0]), [0.9, 0.9, 1.6])
        )

    def test_voxel_spacing_comes_from_the_affine_column_norms(self):
        np.testing.assert_allclose(
            roi_stats.voxel_spacing(affine_of((0.3, 0.35, 0.4))), [0.3, 0.35, 0.4]
        )


class PointTests(SimpleTestCase):
    def setUp(self):
        self.volume = np.arange(8 * 8 * 8, dtype=np.int16).reshape(8, 8, 8)
        self.affine = affine_of((1.0, 1.0, 1.0))

    def test_a_probe_reads_the_voxel_it_was_dropped_on(self):
        # Voxel (2, 3, 4) holds 2*64 + 3*8 + 4 = 156, and sits at RAS (2, 3, 4) =
        # LPS (-2, -3, 4).
        result = roi_stats.statistics_for_geometry(
            self.volume,
            self.affine,
            geometry_type=Geometry3DType.POINT,
            points=[[-2.0, -3.0, 4.0]],
        )
        self.assertEqual(result["count"], 1)
        self.assertAlmostEqual(result["mean"], 156.0)
        self.assertAlmostEqual(result["stddev"], 0.0)

    def test_a_probe_rounds_rather_than_truncating(self):
        """Truncation biases every reading half a voxel towards the origin."""
        # LPS (-1.6, -0.0, 0.0) is voxel x = 1.6, which rounds to 2, not 1.
        result = roi_stats.statistics_for_geometry(
            self.volume,
            self.affine,
            geometry_type=Geometry3DType.POINT,
            points=[[-1.6, 0.0, 0.0]],
        )
        self.assertAlmostEqual(result["mean"], float(self.volume[2, 0, 0]))

    def test_a_probe_outside_the_volume_reports_nothing_rather_than_zero(self):
        """None is the honest answer; zero would be a measurement."""
        for outside in ([-100.0, 0.0, 0.0], [0.0, 0.0, 1000.0], [5.0, 0.0, 0.0]):
            self.assertIsNone(
                roi_stats.statistics_for_geometry(
                    self.volume,
                    self.affine,
                    geometry_type=Geometry3DType.POINT,
                    points=[outside],
                ),
                f"{outside} is outside the volume",
            )


class SphereTests(SimpleTestCase):
    def test_a_sphere_selects_by_millimetres_not_by_index(self):
        """Anisotropic on purpose: an index-space radius reports an ellipsoid.

        With 1 x 1 x 4 mm voxels, a 1.5 mm sphere reaches one voxel either side along
        x and y and none along z. Applying the radius in index space would reach one
        voxel along z too -- four millimetres away -- and call it a sphere.
        """
        volume = np.zeros((9, 9, 9), dtype=np.int16)
        affine = affine_of((1.0, 1.0, 4.0))
        centre_lps = [-4.0, -4.0, 16.0]  # voxel (4, 4, 4)

        indices = roi_stats.sphere_indices(volume.shape, affine, centre_lps, 1.5)
        self.assertGreater(len(indices), 1)
        # Nothing off the centre slice: the nearest is 4 mm away.
        self.assertEqual(set(indices[:, 2].tolist()), {4})
        # The in-plane cross is there.
        as_set = {tuple(row) for row in indices.tolist()}
        for expected in [(4, 4, 4), (3, 4, 4), (5, 4, 4), (4, 3, 4), (4, 5, 4)]:
            self.assertIn(expected, as_set)

    def test_a_sphere_averages_what_it_covers(self):
        volume = np.zeros((8, 8, 8), dtype=np.int16)
        volume[3:6, 3:6, 3:6] = 100
        affine = affine_of((1.0, 1.0, 1.0))
        # Centred on voxel (4,4,4) = RAS (4,4,4) = LPS (-4,-4,4), radius 1 mm reaches
        # the six face neighbours plus the centre, all inside the 100 block.
        result = roi_stats.statistics_for_geometry(
            volume,
            affine,
            geometry_type=Geometry3DType.SPHERE,
            points=[[-4.0, -4.0, 4.0]],
            attributes={"radius": 1.0},
        )
        self.assertEqual(result["count"], 7)
        self.assertAlmostEqual(result["mean"], 100.0)
        self.assertAlmostEqual(result["min"], 100.0)
        self.assertAlmostEqual(result["max"], 100.0)

    def test_a_sphere_straddling_an_edge_reports_the_mixture(self):
        volume = np.zeros((8, 8, 8), dtype=np.int16)
        volume[4:, :, :] = 200
        result = roi_stats.statistics_for_geometry(
            volume,
            affine_of((1.0, 1.0, 1.0)),
            geometry_type=Geometry3DType.SPHERE,
            points=[[-4.0, -4.0, 4.0]],
            attributes={"radius": 1.0},
        )
        # Six of the seven voxels are at x >= 4, one (x=3) is not.
        self.assertEqual(result["count"], 7)
        self.assertAlmostEqual(result["mean"], 200.0 * 6 / 7)
        self.assertAlmostEqual(result["min"], 0.0)
        self.assertAlmostEqual(result["max"], 200.0)
        self.assertGreater(result["stddev"], 0.0)

    def test_a_sphere_needs_a_finite_radius(self):
        with self.assertRaises(ValueError):
            roi_stats.indices_for_geometry(
                (8, 8, 8),
                affine_of((1.0, 1.0, 1.0)),
                geometry_type=Geometry3DType.SPHERE,
                points=[[0, 0, 0]],
                attributes={},
            )

    def test_a_zero_radius_sphere_selects_nothing(self):
        indices = roi_stats.sphere_indices(
            (8, 8, 8), affine_of((1.0, 1.0, 1.0)), [-4.0, -4.0, 4.0], 0.0
        )
        self.assertEqual(len(indices), 0)

    def test_a_sphere_entirely_outside_the_volume_is_empty(self):
        indices = roi_stats.sphere_indices(
            (8, 8, 8), affine_of((1.0, 1.0, 1.0)), [500.0, 500.0, 500.0], 2.0
        )
        self.assertEqual(len(indices), 0)


class BoxTests(SimpleTestCase):
    def test_a_box_selects_the_voxels_between_its_corners(self):
        volume = np.zeros((8, 8, 8), dtype=np.int16)
        volume[2:5, 2:5, 2:5] = 50
        # LPS corners for the RAS block x,y,z in [2, 4].
        indices = roi_stats.box_indices(
            volume.shape, affine_of((1.0, 1.0, 1.0)), [[-2.0, -2.0, 2.0], [-4.0, -4.0, 4.0]]
        )
        self.assertEqual(len(indices), 27)
        values = roi_stats.sample_values(volume, indices)
        np.testing.assert_allclose(values, 50.0)

    def test_a_box_given_its_corners_in_any_order_is_the_same_box(self):
        affine = affine_of((1.0, 1.0, 1.0))
        forward = roi_stats.box_indices((8, 8, 8), affine, [[-2.0, -2.0, 2.0], [-4.0, -4.0, 4.0]])
        reversed_corners = roi_stats.box_indices(
            (8, 8, 8), affine, [[-4.0, -4.0, 4.0], [-2.0, -2.0, 2.0]]
        )
        self.assertEqual(
            {tuple(r) for r in forward.tolist()}, {tuple(r) for r in reversed_corners.tolist()}
        )

    def test_a_box_is_clipped_to_the_volume(self):
        indices = roi_stats.box_indices(
            (8, 8, 8), affine_of((1.0, 1.0, 1.0)), [[0.0, 0.0, 0.0], [-100.0, -100.0, 100.0]]
        )
        self.assertEqual(len(indices), 8 * 8 * 8)


class PolygonTests(SimpleTestCase):
    def test_a_planar_rectangle_covers_the_voxels_under_it(self):
        volume = np.zeros((8, 8, 8), dtype=np.int16)
        volume[:, :, 4] = 70
        affine = affine_of((1.0, 1.0, 1.0))
        # A 2x2 mm square on the z = 4 plane, RAS x,y in [2, 4]. In LPS the corners are
        # negated in x and y.
        corners = [
            [-2.0, -2.0, 4.0],
            [-4.0, -2.0, 4.0],
            [-4.0, -4.0, 4.0],
            [-2.0, -4.0, 4.0],
        ]
        result = roi_stats.statistics_for_geometry(
            volume, affine, geometry_type=Geometry3DType.POLYLINE, points=corners
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["mean"], 70.0)
        # 3x3 voxel centres lie in a 2 mm square inclusive of its edges.
        self.assertEqual(result["count"], 9)

    def test_the_polygon_stays_on_its_own_plane(self):
        """A slab-shaped selection would average in neighbouring slices."""
        volume = np.zeros((8, 8, 8), dtype=np.int16)
        volume[:, :, 4] = 70
        volume[:, :, 3] = 1000
        volume[:, :, 5] = 1000
        corners = [
            [-2.0, -2.0, 4.0],
            [-4.0, -2.0, 4.0],
            [-4.0, -4.0, 4.0],
            [-2.0, -4.0, 4.0],
        ]
        result = roi_stats.statistics_for_geometry(
            volume, affine_of((1.0, 1.0, 1.0)), geometry_type=Geometry3DType.POLYLINE, points=corners
        )
        self.assertAlmostEqual(result["mean"], 70.0)
        self.assertAlmostEqual(result["max"], 70.0, msg="a neighbouring slice leaked in")

    def test_an_oblique_polygon_is_sampled_on_its_own_plane(self):
        # A triangle tilted out of every axis plane. The assertion is modest on purpose
        # -- what matters is that it selects voxels at all and stays inside the volume.
        volume = np.arange(8 * 8 * 8, dtype=np.int16).reshape(8, 8, 8)
        indices = roi_stats.polygon_indices(
            volume.shape,
            affine_of((1.0, 1.0, 1.0)),
            [[-1.0, -1.0, 1.0], [-5.0, -1.0, 2.0], [-3.0, -5.0, 5.0]],
        )
        self.assertGreater(len(indices), 5)
        self.assertTrue(np.all(indices >= 0))
        self.assertTrue(np.all(indices < 8))
        # De-duplicated: oversampling must not weight a voxel twice in the mean.
        self.assertEqual(len(indices), len({tuple(row) for row in indices.tolist()}))

    def test_a_two_point_polyline_has_no_interior(self):
        """A length measurement is a line; asking for its mean is asking for nothing."""
        indices = roi_stats.polygon_indices(
            (8, 8, 8), affine_of((1.0, 1.0, 1.0)), [[0.0, 0.0, 0.0], [-4.0, 0.0, 0.0]]
        )
        self.assertEqual(len(indices), 0)

    def test_a_degenerate_polygon_selects_nothing(self):
        # Three collinear points define no plane.
        indices = roi_stats.polygon_indices(
            (8, 8, 8),
            affine_of((1.0, 1.0, 1.0)),
            [[0.0, 0.0, 0.0], [-1.0, 0.0, 0.0], [-2.0, 0.0, 0.0]],
        )
        self.assertEqual(len(indices), 0)


class ValueTests(SimpleTestCase):
    """Statistics come back in modality units, and non-finite samples are dropped."""

    def test_the_residual_lut_is_applied_to_every_sample(self):
        volume = np.full((4, 4, 4), 1524, dtype=np.uint16)
        indices = np.array([[0, 0, 0], [1, 1, 1]])
        values = roi_stats.sample_values(
            volume, indices, rescale_slope=1.0, rescale_intercept=-1024.0
        )
        np.testing.assert_allclose(values, [500.0, 500.0])

    def test_a_mean_is_reported_in_hounsfield_not_in_stored_units(self):
        volume = np.full((4, 4, 4), 1524, dtype=np.uint16)
        result = roi_stats.statistics_for_geometry(
            volume,
            affine_of((1.0, 1.0, 1.0)),
            geometry_type=Geometry3DType.POINT,
            points=[[-1.0, -1.0, 1.0]],
            rescale_slope=1.0,
            rescale_intercept=-1024.0,
        )
        self.assertAlmostEqual(result["mean"], 500.0)
        self.assertNotAlmostEqual(result["mean"], 1524.0)

    def test_non_finite_samples_are_dropped_rather_than_propagated(self):
        # A masked-out MRI background written as NaN would otherwise make every
        # statistic NaN.
        stats = roi_stats.statistics([1.0, 2.0, float("nan"), 3.0, float("inf")])
        self.assertEqual(stats["count"], 3, "the count reports what was actually used")
        self.assertAlmostEqual(stats["mean"], 2.0)
        self.assertAlmostEqual(stats["max"], 3.0)

    def test_statistics_over_nothing_is_none(self):
        self.assertIsNone(roi_stats.statistics([]))
        self.assertIsNone(roi_stats.statistics([float("nan")]))

    def test_the_standard_deviation_is_the_population_one(self):
        """A sample sd would differ visibly on a small ROI and match nothing on screen."""
        stats = roi_stats.statistics([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
        self.assertAlmostEqual(stats["mean"], 5.0)
        self.assertAlmostEqual(stats["stddev"], 2.0)  # ddof=0; ddof=1 would be ~2.138

    def test_every_reported_statistic_is_present(self):
        stats = roi_stats.statistics([1.0, 2.0, 3.0])
        self.assertEqual(sorted(stats), sorted(roi_stats.STATISTIC_KINDS))


class UnsupportedGeometryTests(SimpleTestCase):
    def test_a_geometry_with_no_voxel_footprint_is_an_error(self):
        with self.assertRaises(ValueError):
            roi_stats.indices_for_geometry(
                (8, 8, 8),
                affine_of((1.0, 1.0, 1.0)),
                geometry_type=Geometry3DType.PLANE,
                points=[[0, 0, 0], [1, 0, 0], [0, 1, 0]],
            )
