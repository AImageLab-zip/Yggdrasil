"""Phase 9: what the export writes as SEG, SR and RTSTRUCT.

Three properties, each of which fails silently without a test.

An **uncalibrated measurement written as millimetres** is a wrong number a clinician
cannot detect. ``MeasurementItem`` refuses that in the database; this file asserts the
refusal survives the trip into an interchange format, where the temptation to convert is
strongest because DICOM has no pixel unit.

A **SEG whose frames are in the wrong order** renders as a segmentation of the other end
of the jaw, and looks entirely plausible. The alignment check and the slice-direction
comparison are asserted here because ``highdicom`` matches frames positionally and asks
no questions.

An **RTSTRUCT nobody read back** is the risk register's item 13. Its premise turned out
to be wrong -- ``highdicom`` has no RTSTRUCT writer at all, so there was no
under-exercised library to be wary of -- which makes the round trip the only evidence
there is: every object written here is re-read with ``pydicom`` and its contours are
re-derived and compared to the rows they came from.
"""

import io

import numpy as np
from django.test import TestCase, override_settings
from pydicom import dcmread
from pydicom.sr.coding import Code

from annotations.constants import (
    AnnotationOrigin,
    CoordinateSystem,
    Geometry3DType,
    MeasurementKind,
    MeasurementUnit,
)
from annotations.models import (
    AnnotationSet,
    MeasurementItem,
    SourceResource,
    SpatialAnnotation3DItem,
)
from annotations.services import attach_target, get_or_create_set, record_revision
from common.dicom.deidentify import deidentify
from common.dicom.models import DicomSeries, series_for_resource
from common.interop import (
    InteropUnavailable,
    build_rtstruct,
    build_seg,
    build_sr,
    derived_uid,
)
from common.models import FileRegistry, Project
from common.tests_dicom import synthetic_instance
from maxillo.models import Folder, Patient


def _series_datasets(count=4, *, series_uid="1.2.826.0.1.3680043.9.7.1"):
    """A stored series as ``instance_datasets`` hands it back: in slice order.

    Put through the *real* de-identifier rather than used raw. That is not ceremony:
    ``deidentify`` rebuilds the dataset from the keep-list, and what it emits is the
    only thing an export will ever read. A fixture assembled by hand can carry
    attributes the whitelist drops -- or, as the first run of this file found, *lack*
    ones it always adds, so a writer that reads ``PatientName`` off its source passes
    against production bytes and fails here for a reason that has nothing to do with
    the code under test.
    """
    return [
        deidentify(
            synthetic_instance(
                sop_instance_uid=f"{series_uid}.{index + 1}",
                series_instance_uid=series_uid,
                instance_number=index + 1,
                position=(0.0, 0.0, float(index)),
            ),
            patient_id=1,
        )
        for index in range(count)
    ]


@override_settings(DICOM_UID_HMAC_KEY="interop-test-key")
class InteropBase(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="io", slug="io", domain="maxillo")
        self.folder = Folder.objects.create(name="F", project=self.project)
        self.patient = Patient.objects.create(project=self.project, folder=self.folder)
        self.row = FileRegistry.objects.create(
            domain="maxillo",
            patient=self.patient,
            file_type="cbct_raw",
            file_path="dicom/series/",
            file_size=1,
        )
        self.datasets = _series_datasets()
        self.series = DicomSeries.objects.create(
            file=self.row,
            series_instance_uid=self.datasets[0].SeriesInstanceUID,
            study_instance_uid=self.datasets[0].StudyInstanceUID,
            frame_of_reference_uid=self.datasets[0].FrameOfReferenceUID,
            instance_count=len(self.datasets),
            rows=4,
            columns=4,
        )
        self.resource = SourceResource.objects.create(
            kind="logical_volume",
            identity_key=f"logical_volume:{self.row.pk}",
            file=self.row,
        )
        self.set = get_or_create_set(self.patient, kind="measurements")
        attach_target(self.set, self.resource, role="volume")
        self.revision = record_revision(self.set, origin=AnnotationOrigin.MANUAL)

    def _shape(self, geometry_type, points, **kwargs):
        return SpatialAnnotation3DItem.objects.create(
            revision=self.revision,
            target=self.set.targets.first(),
            geometry_type=geometry_type,
            coordinate_system=CoordinateSystem.PATIENT_LPS_MM,
            points=points,
            frame_of_reference_uid=self.series.frame_of_reference_uid,
            **kwargs,
        )

    def _measurement(self, *, kind, value, unit, calibrated, shape=None):
        return MeasurementItem.objects.create(
            revision=self.revision,
            target=self.set.targets.first(),
            kind=kind,
            value=value,
            unit=unit,
            is_calibrated=calibrated,
            spatial_3d_item=shape,
        )


class SeriesResolutionTests(InteropBase):
    """F21: which resource the viewer actually writes, and what that broke."""

    def test_a_logical_volume_over_a_dicom_row_resolves_to_its_series(self):
        # This is the shape the volume grid produces on every real save: a
        # `logical_volume` resource with a blank series_instance_uid, over the
        # FileRegistry row the series is stored as. Asking the UID column alone --
        # which is what the seal used to do -- answers "not DICOM" here.
        self.assertEqual(self.resource.series_instance_uid, "")
        self.assertEqual(series_for_resource(self.resource), self.series)

    def test_the_ingest_side_resource_still_resolves(self):
        ingest_resource = SourceResource.objects.create(
            kind="dicom_series",
            identity_key=f"dicom_series:{self.series.series_instance_uid}",
            file=self.row,
            series_instance_uid=self.series.series_instance_uid,
        )
        self.assertEqual(series_for_resource(ingest_resource), self.series)

    def test_a_resource_with_no_dicom_under_it_resolves_to_nothing(self):
        other = FileRegistry.objects.create(
            domain="maxillo", patient=self.patient, file_type="cbct_raw",
            file_path="scan.nii.gz", file_size=1,
        )
        resource = SourceResource.objects.create(
            kind="logical_volume", identity_key=f"logical_volume:{other.pk}", file=other
        )
        self.assertIsNone(series_for_resource(resource))

    def test_human_work_on_a_grid_saved_resource_seals_the_series(self):
        """The F21 regression: the seal has to fire on the resource the viewer writes.

        Before the fix `_seal_dicom_sources` filtered on `kind=dicom_series` and read
        `series_instance_uid` off the target, so a measurement saved through the volume
        grid -- the only way anyone actually annotates -- left the series unsealed and
        every instance rewritable underneath its own annotations.
        """
        record_revision(self.set, origin=AnnotationOrigin.MANUAL)
        self.series.refresh_from_db()
        self.assertIsNotNone(self.series.sealed_at)


class MeasurementReportTests(InteropBase):
    """The calibration distinction, carried into an interchange format."""

    def test_a_calibrated_length_is_millimetres(self):
        self._measurement(
            kind=MeasurementKind.LENGTH, value=12.4,
            unit=MeasurementUnit.MM, calibrated=True,
        )
        document = build_sr(self.revision, self.series, self.datasets)
        measurement = self._only_measurement(document)
        units = measurement.MeasuredValueSequence[0].MeasurementUnitsCodeSequence[0]
        self.assertEqual(units.CodeValue, "mm")
        self.assertEqual(float(measurement.MeasuredValueSequence[0].NumericValue), 12.4)

    def test_an_uncalibrated_length_stays_pixels(self):
        """The number is not converted, and the unit is what says so.

        Converting it would be the exact fabrication `is_calibrated` exists to prevent:
        the receiving system has no way to tell 12.4 pixels dressed as 12.4 mm from a
        real measurement.
        """
        self._measurement(
            kind=MeasurementKind.LENGTH, value=12.4,
            unit=MeasurementUnit.PX, calibrated=False,
        )
        document = build_sr(self.revision, self.series, self.datasets)
        value_item = self._only_measurement(document).MeasuredValueSequence[0]
        self.assertEqual(value_item.MeasurementUnitsCodeSequence[0].CodeValue, "{pixels}")
        self.assertEqual(float(value_item.NumericValue), 12.4)

    def test_an_uncalibrated_value_is_not_marked_unusable(self):
        """Numeric Value Qualifier is CID 42: it says the number is *not a number*.

        An earlier draft put a "not calibrated" code there. A conforming receiver reads
        (0040,A301) as "there is no usable value here", so that encoding discarded the
        measurement in the act of trying to caveat it -- and a strict one rejects a code
        outside CID 42 altogether. There is no standard concept for "uncalibrated", so
        the unit carries it and this attribute stays absent.
        """
        self._measurement(
            kind=MeasurementKind.LENGTH, value=12.4,
            unit=MeasurementUnit.PX, calibrated=False,
        )
        document = build_sr(self.revision, self.series, self.datasets)
        measurement = self._only_measurement(document)
        self.assertNotIn("NumericValueQualifierCodeSequence", measurement)

    def test_geometry_is_carried_as_scoord3d_in_the_series_frame_of_reference(self):
        shape = self._shape(
            Geometry3DType.POLYLINE, [[0.0, 0.0, 0.0], [1.0, 2.0, 3.0]]
        )
        self._measurement(
            kind=MeasurementKind.LENGTH, value=3.7,
            unit=MeasurementUnit.MM, calibrated=True, shape=shape,
        )
        document = build_sr(self.revision, self.series, self.datasets)
        scoords = self._content(document, "SCOORD3D")
        self.assertEqual(len(scoords), 1)
        self.assertEqual(
            scoords[0].ReferencedFrameOfReferenceUID,
            self.series.frame_of_reference_uid,
        )
        self.assertEqual(
            [float(v) for v in scoords[0].GraphicData], [0.0, 0.0, 0.0, 1.0, 2.0, 3.0]
        )

    def test_ras_geometry_is_dropped_rather_than_sign_flipped(self):
        """LPS and RAS differ by two sign flips; converting silently mirrors the shape."""
        shape = self._shape(Geometry3DType.POLYLINE, [[0.0, 0.0, 0.0], [1.0, 2.0, 3.0]])
        SpatialAnnotation3DItem.objects.filter(pk=shape.pk).update(
            coordinate_system=CoordinateSystem.PATIENT_RAS_MM
        )
        shape.refresh_from_db()
        self._measurement(
            kind=MeasurementKind.LENGTH, value=3.7,
            unit=MeasurementUnit.MM, calibrated=True, shape=shape,
        )
        document = build_sr(self.revision, self.series, self.datasets)
        self.assertEqual(self._content(document, "SCOORD3D"), [])

    def test_a_revision_with_no_measurements_produces_nothing(self):
        self.assertIsNone(build_sr(self.revision, self.series, self.datasets))

    def test_a_series_with_no_frame_of_reference_is_refused(self):
        DicomSeries.objects.filter(pk=self.series.pk).update(frame_of_reference_uid="")
        self.series.refresh_from_db()
        self._measurement(
            kind=MeasurementKind.LENGTH, value=1.0,
            unit=MeasurementUnit.MM, calibrated=True,
        )
        with self.assertRaises(InteropUnavailable):
            build_sr(self.revision, self.series, self.datasets)

    def test_the_document_is_stable_across_exports(self):
        """Derived UIDs, so a re-export is recognisably the same object."""
        self._measurement(
            kind=MeasurementKind.LENGTH, value=1.0,
            unit=MeasurementUnit.MM, calibrated=True,
        )
        first = build_sr(self.revision, self.series, self.datasets)
        second = build_sr(self.revision, self.series, self.datasets)
        self.assertEqual(first.SOPInstanceUID, second.SOPInstanceUID)
        self.assertEqual(first.SeriesInstanceUID, second.SeriesInstanceUID)

    # --- helpers ----------------------------------------------------------------

    @staticmethod
    def _content(dataset, value_type):
        found = []

        def walk(sequence):
            for item in sequence:
                if getattr(item, "ValueType", "") == value_type:
                    found.append(item)
                walk(getattr(item, "ContentSequence", []) or [])

        walk(dataset.ContentSequence)
        return found

    def _only_measurement(self, document):
        numeric = self._content(document, "NUM")
        self.assertEqual(len(numeric), 1)
        return numeric[0]


class SegmentationTests(InteropBase):
    """Frame order and grid alignment -- the two ways a SEG is silently wrong."""

    def _labelmap(self, frames=4, rows=4, columns=4):
        # nibabel order: (columns, rows, slices).
        volume = np.zeros((columns, rows, frames), dtype=np.uint8)
        volume[1, 1, 0] = 1  # a voxel on the first slice only
        volume[2, 2, :] = 2
        return volume

    def _affine(self, z_sign=1.0):
        affine = np.eye(4)
        affine[2, 2] = z_sign
        return affine

    def test_segments_are_renumbered_and_keep_their_original_value(self):
        document = build_seg(
            self._labelmap(), self._affine(), self.series, self.datasets
        )
        numbers = [s.SegmentNumber for s in document.SegmentSequence]
        self.assertEqual(numbers, [1, 2])
        # Renumbering 1..N is required by the IOD; the stored label value is what a
        # consumer needs to map back, so it survives as the tracking id.
        self.assertEqual(
            [s.TrackingID for s in document.SegmentSequence], ["label-1", "label-2"]
        )

    def test_a_mismatched_grid_is_refused_rather_than_resampled(self):
        with self.assertRaises(InteropUnavailable) as ctx:
            build_seg(
                np.zeros((4, 4, 9), dtype=np.uint8),
                self._affine(), self.series, self.datasets,
            )
        self.assertIn("not the same grid", str(ctx.exception))

    def test_slice_direction_follows_the_stored_positions(self):
        """A flipped affine flips the array, so frame 1 is still instance 1.

        The synthetic series runs +Z with instance number, so an affine that runs -Z
        describes the same voxels in the opposite order. Getting this wrong exports a
        segmentation of the other end of the volume and looks entirely plausible.
        """
        volume = self._labelmap()
        forward = build_seg(volume, self._affine(1.0), self.series, self.datasets)
        reversed_ = build_seg(volume, self._affine(-1.0), self.series, self.datasets)
        self.assertNotEqual(
            forward.pixel_array.tobytes(), reversed_.pixel_array.tobytes()
        )

    def test_an_empty_labelmap_produces_nothing(self):
        self.assertIsNone(
            build_seg(
                np.zeros((4, 4, 4), dtype=np.uint8),
                self._affine(), self.series, self.datasets,
            )
        )

    def test_the_source_series_is_referenced(self):
        document = build_seg(
            self._labelmap(), self._affine(), self.series, self.datasets
        )
        referenced = document.ReferencedSeriesSequence[0]
        self.assertEqual(referenced.SeriesInstanceUID, self.series.series_instance_uid)
        self.assertEqual(
            len(referenced.ReferencedInstanceSequence), len(self.datasets)
        )


class RTStructTests(InteropBase):
    """Risk 13, discharged by reading every written object back.

    highdicom has no RTSTRUCT writer, so there was never an under-exercised library to
    be wary of -- the module builds the IOD directly and these tests are the evidence.
    """

    def _written(self):
        document = build_rtstruct(self.revision, self.series, self.datasets)
        if document is None:
            return None
        buffer = io.BytesIO()
        document.save_as(buffer, enforce_file_format=True)
        buffer.seek(0)
        return dcmread(buffer)

    def test_a_polyline_round_trips_to_the_points_it_came_from(self):
        points = [[1.5, 2.5, 0.0], [3.5, 4.5, 0.0], [5.5, 6.5, 0.0]]
        self._shape(Geometry3DType.POLYLINE, points)
        document = self._written()

        contour = document.ROIContourSequence[0].ContourSequence[0]
        self.assertEqual(contour.ContourGeometricType, "OPEN_PLANAR")
        self.assertEqual(contour.NumberOfContourPoints, 3)
        recovered = np.asarray([float(v) for v in contour.ContourData]).reshape(-1, 3)
        np.testing.assert_allclose(recovered, np.asarray(points), atol=1e-6)

    def test_the_three_sequences_agree_element_for_element(self):
        """A structure set whose sequences disagree shows as an empty list, not an error."""
        self._shape(Geometry3DType.POLYLINE, [[0, 0, 0], [1, 0, 0], [1, 1, 0]])
        self._shape(Geometry3DType.PLANE, [[0, 0, 1], [2, 0, 1], [2, 2, 1]])
        document = self._written()

        roi_numbers = [r.ROINumber for r in document.StructureSetROISequence]
        contour_numbers = [c.ReferencedROINumber for c in document.ROIContourSequence]
        observed = [o.ReferencedROINumber for o in document.RTROIObservationsSequence]
        self.assertEqual(roi_numbers, [1, 2])
        self.assertEqual(contour_numbers, roi_numbers)
        self.assertEqual(observed, roi_numbers)

    def test_a_plane_is_a_closed_contour_and_a_polyline_is_not(self):
        self._shape(Geometry3DType.PLANE, [[0, 0, 0], [2, 0, 0], [2, 2, 0]])
        document = self._written()
        self.assertEqual(
            document.ROIContourSequence[0].ContourSequence[0].ContourGeometricType,
            "CLOSED_PLANAR",
        )

    def test_a_sphere_is_omitted_rather_than_tessellated(self):
        """DICOM has no sphere ROI. Approximating one files a rendering choice as data."""
        self._shape(
            Geometry3DType.SPHERE, [[0.0, 0.0, 0.0]], attributes={"radius_mm": 5.0}
        )
        self.assertIsNone(self._written())

    def test_a_box_is_omitted_rather_than_approximated(self):
        self._shape(Geometry3DType.BOX, [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]])
        self.assertIsNone(self._written())

    def test_every_contour_names_the_instance_nearest_to_it(self):
        """A planar contour must name its image; the position picks it, not an assumption."""
        self._shape(Geometry3DType.POLYLINE, [[0, 0, 3.0], [1, 0, 3.0], [1, 1, 3.0]])
        document = self._written()
        referenced = (
            document.ROIContourSequence[0]
            .ContourSequence[0]
            .ContourImageSequence[0]
            .ReferencedSOPInstanceUID
        )
        self.assertEqual(referenced, self.datasets[3].SOPInstanceUID)

    def test_the_frame_of_reference_is_the_series_own(self):
        self._shape(Geometry3DType.POLYLINE, [[0, 0, 0], [1, 0, 0], [1, 1, 0]])
        document = self._written()
        frame = document.ReferencedFrameOfReferenceSequence[0]
        self.assertEqual(frame.FrameOfReferenceUID, self.series.frame_of_reference_uid)
        study = frame.RTReferencedStudySequence[0]
        self.assertEqual(
            study.RTReferencedSeriesSequence[0].SeriesInstanceUID,
            self.series.series_instance_uid,
        )

    def test_it_is_a_readable_rt_structure_set(self):
        self._shape(Geometry3DType.POLYLINE, [[0, 0, 0], [1, 0, 0], [1, 1, 0]])
        document = self._written()
        self.assertEqual(document.Modality, "RTSTRUCT")
        self.assertEqual(document.SOPClassUID, "1.2.840.10008.5.1.4.1.1.481.3")
        self.assertEqual(
            document.file_meta.MediaStorageSOPInstanceUID, document.SOPInstanceUID
        )


class DerivedUidTests(TestCase):
    @override_settings(DICOM_UID_HMAC_KEY="interop-test-key")
    def test_purposes_do_not_collide(self):
        """One revision of one series produces three objects; they need three names."""
        uids = {
            derived_uid(purpose, "series", 1)
            for purpose in ("sr-instance", "seg-instance", "rtstruct-instance")
        }
        self.assertEqual(len(uids), 3)
        for uid in uids:
            self.assertTrue(uid.startswith("2.25."))
            self.assertLessEqual(len(uid), 64)
