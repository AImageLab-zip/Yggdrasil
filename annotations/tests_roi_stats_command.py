"""The ROI-statistics sweep, end to end against real NIfTI bytes.

``annotations.roi_stats`` is covered separately as pure arithmetic. What is tested
here is everything around it: which geometry is picked up, that a volume shared by
several ROIs is downloaded once, that the sweep survives one bad file, that it is
idempotent, and that a number only claims to be Hounsfield when it has earned it.

Object storage is patched at the module boundary -- the house pattern from
``annotations.tests_commands`` -- but the *file* is real: nibabel writes it and nibabel
reads it back, so the scaling behaviour this command depends on is the genuine one and
not a mock's idea of it.
"""

import tempfile
import uuid
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from unittest import mock

import nibabel as nib
import numpy as np
from django.core.management import call_command
from django.test import TestCase

from annotations import services
from annotations.constants import (
    CoordinateSystem,
    Geometry3DType,
    MeasurementKind,
    MeasurementUnit,
)
from annotations.models import MeasurementItem, SpatialAnnotation3DItem
from common.models import FileRegistry, Project
from maxillo.models import Folder, Patient

COMMAND = "annotations_compute_roi_stats"
MODULE = f"annotations.management.commands.{COMMAND}"


def _run(**options):
    out, err = StringIO(), StringIO()
    call_command(COMMAND, stdout=out, stderr=err, **options)
    return out.getvalue() + err.getvalue()


class RoiStatsCommandTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tempdir = tempfile.TemporaryDirectory()
        cls.volume_path = Path(cls._tempdir.name) / "volume.nii"

        # A 12-cubed volume: background 0, a 3x3x3 block of stored value 1524 centred
        # on voxel (6, 6, 6). With scl_inter = -1024 that block is 500 HU.
        data = np.zeros((12, 12, 12), dtype=np.int16)
        data[5:8, 5:8, 5:8] = 1524
        image = nib.Nifti1Image(data, affine=np.eye(4))
        image.header.set_slope_inter(1, -1024)
        nib.save(image, str(cls.volume_path))

    @classmethod
    def tearDownClass(cls):
        cls._tempdir.cleanup()
        super().tearDownClass()

    def setUp(self):
        suffix = uuid.uuid4().hex[:8]
        self.project = Project.objects.create(
            name=f"roi-{suffix}", slug=f"roi-{suffix}", domain="maxillo"
        )
        self.folder = Folder.objects.create(name="mx", project=self.project)
        self.patient = Patient.objects.create(
            patient_id=9401, folder=self.folder, project=self.project
        )
        self.file = FileRegistry.objects.create(
            patient=self.patient,
            file_type="cbct_raw",
            file_path="maxillo/cbct_raw/volume.nii",
            file_size=1,
            file_hash="0" * 64,
            domain="maxillo",
        )

        self.downloads = []

        @contextmanager
        def fake_download(path, suffix=""):
            self.downloads.append(path)
            yield str(self.volume_path)

        for target, replacement in (
            ("download_to_tempfile", fake_download),
            ("artifact_exists", lambda path: True),
        ):
            patcher = mock.patch(f"{MODULE}.{target}", replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- fixtures -----------------------------------------------------------

    def _geometry(
        self,
        *,
        geometry_type=Geometry3DType.SPHERE,
        points=None,
        attributes=None,
        coordinate_system=CoordinateSystem.PATIENT_LPS_MM,
        file_obj=None,
    ):
        """One stored ROI on this patient, written through the service layer.

        Every ROI goes on the **same** revision, because that is what a session
        produces: one save holding everything on screen. Creating a revision per ROI
        would put all but the last on superseded history, which the sweep correctly
        ignores -- and the test would then be asserting against data no viewer shows.
        """
        annotation_set = services.get_or_create_set(self.patient, services.MEASUREMENTS_KIND)
        resource = services.register_logical_volume(file_obj or self.file)
        target = services.attach_target(annotation_set, resource, primary=False)
        revision = annotation_set.revisions.order_by("-revision_number").first()
        if revision is None:
            revision = services.record_revision(annotation_set)
        return services.add_spatial_3d(
            revision,
            target,
            geometry_type=geometry_type,
            coordinate_system=coordinate_system,
            # Voxel (6, 6, 6) with an identity affine is RAS (6, 6, 6) = LPS (-6, -6, 6).
            points=points if points is not None else [[-6.0, -6.0, 6.0]],
            attributes=attributes if attributes is not None else {"radius": 1.0},
        )

    def _stats_for(self, item):
        return {
            entry.kind: entry
            for entry in MeasurementItem.objects.filter(spatial_3d_item=item)
        }

    # -- the happy path -----------------------------------------------------

    def test_a_sphere_gets_five_statistics_in_modality_units(self):
        item = self._geometry()
        _run()

        stats = self._stats_for(item)
        self.assertEqual(
            sorted(stats),
            sorted(
                [
                    MeasurementKind.MEAN,
                    MeasurementKind.STDDEV,
                    MeasurementKind.MIN,
                    MeasurementKind.MAX,
                    MeasurementKind.COUNT,
                ]
            ),
        )
        # The sphere sits entirely inside the 1524-stored block, and nibabel applies
        # scl_inter = -1024, so every voxel is 500 HU.
        self.assertAlmostEqual(float(stats[MeasurementKind.MEAN].value), 500.0, places=3)
        self.assertAlmostEqual(float(stats[MeasurementKind.MIN].value), 500.0, places=3)
        self.assertAlmostEqual(float(stats[MeasurementKind.MAX].value), 500.0, places=3)
        self.assertAlmostEqual(float(stats[MeasurementKind.STDDEV].value), 0.0, places=3)
        self.assertEqual(int(stats[MeasurementKind.COUNT].value), 7)

    def test_the_rescale_is_applied_so_the_number_is_not_the_stored_value(self):
        """F1's whole point, on the server side: 1524 stored is 500 HU."""
        item = self._geometry()
        _run()
        mean = float(self._stats_for(item)[MeasurementKind.MEAN].value)
        self.assertAlmostEqual(mean, 500.0, places=3)
        self.assertNotAlmostEqual(mean, 1524.0, places=3)

    def test_the_sample_count_travels_with_each_statistic(self):
        item = self._geometry()
        _run()
        stats = self._stats_for(item)
        for kind in (MeasurementKind.MEAN, MeasurementKind.STDDEV, MeasurementKind.MIN, MeasurementKind.MAX):
            self.assertEqual(stats[kind].sample_count, 7, kind)
            self.assertEqual(stats[kind].attributes["derived_from"], "roi_stats")

        # The count carries none, and the validator refuses one: the count *is* the
        # sample size, so recording it twice invites the two disagreeing.
        self.assertIsNone(stats[MeasurementKind.COUNT].sample_count)
        self.assertEqual(int(stats[MeasurementKind.COUNT].value), 7)

    def test_a_probe_point_reads_one_voxel(self):
        item = self._geometry(geometry_type=Geometry3DType.POINT, attributes={})
        _run()
        stats = self._stats_for(item)
        self.assertEqual(int(stats[MeasurementKind.COUNT].value), 1)
        self.assertAlmostEqual(float(stats[MeasurementKind.MEAN].value), 500.0, places=3)

    def test_a_volume_shared_by_several_rois_is_downloaded_once(self):
        """Otherwise the sweep is O(annotations) round trips against object storage."""
        annotation_set = services.get_or_create_set(self.patient, services.MEASUREMENTS_KIND)
        resource = services.register_logical_volume(self.file)
        target = services.attach_target(annotation_set, resource, primary=True)
        revision = services.record_revision(annotation_set)
        for offset in (0, 1, -1):
            services.add_spatial_3d(
                revision,
                target,
                geometry_type=Geometry3DType.SPHERE,
                coordinate_system=CoordinateSystem.PATIENT_LPS_MM,
                points=[[-6.0 + offset, -6.0, 6.0]],
                attributes={"radius": 1.0},
            )

        _run()
        self.assertEqual(len(self.downloads), 1, self.downloads)
        self.assertEqual(MeasurementItem.objects.count(), 15)

    # -- idempotence --------------------------------------------------------

    def test_a_second_run_writes_nothing_new(self):
        item = self._geometry()
        _run()
        before = MeasurementItem.objects.count()

        output = _run()
        self.assertEqual(MeasurementItem.objects.count(), before)
        self.assertEqual(len(self._stats_for(item)), 5, "statistics must not double")
        # And it does not even download: nothing was pending.
        self.assertEqual(len(self.downloads), 1, output)

    def test_refresh_recomputes_without_duplicating(self):
        item = self._geometry()
        _run()
        _run(refresh=True)
        self.assertEqual(len(self._stats_for(item)), 5)
        self.assertEqual(len(self.downloads), 2)

    def test_dry_run_writes_nothing(self):
        self._geometry()
        output = _run(dry_run=True)
        self.assertEqual(MeasurementItem.objects.count(), 0)
        self.assertIn("would write", output)

    # -- what it declines to compute ---------------------------------------

    def test_a_voxel_frame_roi_is_skipped_rather_than_placed_arbitrarily(self):
        """Voxel points are already indices; mapping them through the affine's inverse
        would put the ROI somewhere arbitrary and still return a number."""
        item = self._geometry(
            coordinate_system=CoordinateSystem.VOLUME_VOXEL, points=[[6.0, 6.0, 6.0]]
        )
        _run()
        self.assertEqual(self._stats_for(item), {})

    def test_a_ras_frame_roi_is_converted_rather_than_refused(self):
        """Same physical point, two sign flips away. Refusing would strand it."""
        item = self._geometry(
            coordinate_system=CoordinateSystem.PATIENT_RAS_MM, points=[[6.0, 6.0, 6.0]]
        )
        _run()
        stats = self._stats_for(item)
        self.assertAlmostEqual(float(stats[MeasurementKind.MEAN].value), 500.0, places=3)

    def test_an_roi_outside_the_volume_reports_nothing(self):
        item = self._geometry(points=[[-500.0, -500.0, 500.0]])
        _run()
        self.assertEqual(self._stats_for(item), {}, "an empty ROI must not become a zero")

    def test_a_two_point_length_has_no_interior_and_is_skipped(self):
        item = self._geometry(
            geometry_type=Geometry3DType.POLYLINE,
            points=[[-5.0, -5.0, 5.0], [-7.0, -7.0, 7.0]],
            attributes={},
        )
        _run()
        self.assertEqual(self._stats_for(item), {})

    # -- units --------------------------------------------------------------

    def test_cbct_values_are_reported_without_claiming_hounsfield(self):
        """CBCT greyscale is vendor-dependent and is not calibrated HU.

        Calling it HU would dress a relative number up as a physical measurement --
        the same mistake the calibration constraint prevents for lengths.
        """
        item = self._geometry()
        _run()
        self.assertEqual(
            self._stats_for(item)[MeasurementKind.MEAN].unit, MeasurementUnit.NONE
        )

    def test_real_ct_values_are_reported_as_hounsfield(self):
        ct_file = FileRegistry.objects.create(
            patient=self.patient,
            file_type="ct_raw",
            file_path="maxillo/ct_raw/volume.nii",
            file_size=1,
            file_hash="2" * 64,
            domain="maxillo",
        )
        item = self._geometry(file_obj=ct_file)
        _run()
        self.assertEqual(self._stats_for(item)[MeasurementKind.MEAN].unit, MeasurementUnit.HU)

    # -- resilience ---------------------------------------------------------

    def test_one_unreadable_volume_does_not_stop_the_sweep(self):
        """A bucket that is missing one object must not lose the rest of the corpus."""
        good = self._geometry()

        broken_file = FileRegistry.objects.create(
            patient=self.patient,
            file_type="cbct_raw",
            file_path="maxillo/cbct_raw/missing.nii",
            file_size=1,
            file_hash="3" * 64,
            domain="maxillo",
        )
        bad = self._geometry(file_obj=broken_file)

        with mock.patch(
            f"{MODULE}.artifact_exists",
            side_effect=lambda path: not path.endswith("missing.nii"),
        ):
            output = _run()

        self.assertIn("failed 1", output)
        self.assertEqual(len(self._stats_for(good)), 5, "the readable volume still ran")
        self.assertEqual(self._stats_for(bad), {})

    def test_the_limit_bounds_a_sweep(self):
        self._geometry()
        second_file = FileRegistry.objects.create(
            patient=self.patient,
            file_type="cbct_raw",
            file_path="maxillo/cbct_raw/second.nii",
            file_size=1,
            file_hash="4" * 64,
            domain="maxillo",
        )
        self._geometry(file_obj=second_file)

        _run(limit=1)
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(SpatialAnnotation3DItem.objects.count(), 2)

    def test_filtering_by_patient_and_domain(self):
        self._geometry()
        self.assertIn("0 volumes", _run(patients=[999999]))
        self.assertIn("0 volumes", _run(domain="brain"))
        self.assertIn("1 volumes", _run(domain="maxillo"))
