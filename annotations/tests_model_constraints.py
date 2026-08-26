"""The invariants this schema delegates to the database, tested against MySQL.

These are not model-field tests. Every case here covers a rule that is *only*
enforced by DDL, and three of them exist specifically because the obvious way to
write the rule -- ``UniqueConstraint(condition=...)`` -- compiles to nothing on
MySQL, with no partial index and no error (F12). A test that passes on SQLite
would prove nothing about production, so these assert the refusal itself.

The suite runs on MySQL 8 (see ``CONTRIBUTING.md``); if it is ever pointed at a
backend that does not enforce ``CHECK``, the calibration and time-span cases
will start passing writes they should refuse, and that is the signal.
"""

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase

from annotations.constants import (
    CoordinateSystem,
    Geometry2DType,
    MeasurementKind,
    MeasurementUnit,
    PayloadFormat,
    ResourceKind,
    SelectorKind,
)
from annotations.models import (
    AnnotationPayload,
    AnnotationRevision,
    AnnotationSelector,
    AnnotationSet,
    AnnotationTarget,
    Geometry2DItem,
    LabelDefinition,
    LabelSchema,
    MeasurementItem,
    SourceResource,
    TemporalAnnotationItem,
)
from common.models import Project
from maxillo.models import Folder, Patient


class AnnotationSchemaTestCase(TestCase):
    """One maxillo patient with a set, a target and a first revision."""

    @classmethod
    def setUpTestData(cls):
        cls.project, _ = Project.objects.update_or_create(
            slug="maxillo", defaults={"name": "maxillo", "domain": "maxillo"}
        )
        cls.user = User.objects.create_user(username="annotator", password="x")
        cls.folder = Folder.objects.create(name="Constraints", project=cls.project)
        cls.patient = Patient.objects.create(
            name="Constrained", folder=cls.folder, project=cls.project
        )

    def _resource(self, identity_key, kind=ResourceKind.LOGICAL_VOLUME):
        return SourceResource.objects.create(kind=kind, identity_key=identity_key)

    def _set(self, kind="volume_segmentation"):
        annotation_set = AnnotationSet.objects.create(kind=kind, created_by=self.user)
        annotation_set.set_patient(self.patient)
        annotation_set.save()
        return annotation_set

    def _target(self, annotation_set, resource, *, role="volume", primary=True):
        return AnnotationTarget.objects.create(
            annotation_set=annotation_set,
            source_resource=resource,
            role=role,
            primary_slot=1 if primary else None,
        )

    def _revision(self, annotation_set, number=1):
        return AnnotationRevision.objects.create(
            annotation_set=annotation_set, revision_number=number, author=self.user
        )


class RevisionConcurrencyTests(AnnotationSchemaTestCase):
    """``(annotation_set, revision_number)`` is the lost-update guard."""

    def test_two_writers_racing_for_the_same_revision_number_cannot_both_win(self):
        annotation_set = self._set()
        self._revision(annotation_set, 1)

        # Both browsers loaded revision 1 and both compute "mine is 2".
        self._revision(annotation_set, 2)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._revision(annotation_set, 2)

        self.assertEqual(annotation_set.revisions.count(), 2)

    def test_the_same_number_in_a_different_set_is_untouched(self):
        first, second = self._set(), self._set()

        self._revision(first, 1)
        self._revision(second, 1)

        self.assertEqual(AnnotationRevision.objects.filter(revision_number=1).count(), 2)


class PrimaryAndCanonicalSlotTests(AnnotationSchemaTestCase):
    """The F12 workaround: a nullable slot column, not a conditional constraint."""

    def test_many_non_primary_targets_coexist_but_only_one_primary(self):
        annotation_set = self._set()
        volume = self._resource("logical_volume:1")
        segmentation = self._resource("logical_volume:2")
        overlay = self._resource("logical_volume:3")

        self._target(annotation_set, volume, role="volume", primary=True)
        self._target(annotation_set, segmentation, role="segmentation", primary=False)
        self._target(annotation_set, overlay, role="overlay", primary=False)

        # NULLs are distinct in MySQL, so the two non-primaries do not collide...
        self.assertEqual(annotation_set.targets.filter(primary_slot=None).count(), 2)
        # ...while a second primary does.
        with self.assertRaises(IntegrityError), transaction.atomic():
            AnnotationTarget.objects.create(
                annotation_set=annotation_set,
                source_resource=overlay,
                role="second-primary",
                primary_slot=1,
            )

    def test_a_second_primary_is_refused_across_a_role_change_too(self):
        """The uniqueness is on the slot, not on the role that happens to hold it."""
        annotation_set = self._set()
        first = self._resource("logical_volume:10")
        second = self._resource("logical_volume:11")
        self._target(annotation_set, first, role="volume", primary=True)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._target(annotation_set, second, role="image", primary=True)

    def test_one_canonical_payload_per_revision_and_many_derived_ones(self):
        annotation_set = self._set()
        revision = self._revision(annotation_set)

        AnnotationPayload.objects.create(
            revision=revision,
            format=PayloadFormat.YGGDRASIL_JSON,
            canonical_slot=1,
            data={"items": []},
        )
        AnnotationPayload.objects.create(
            revision=revision,
            format=PayloadFormat.CORNERSTONE_STATE,
            data={"annotations": []},
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            AnnotationPayload.objects.create(
                revision=revision,
                format=PayloadFormat.DICOM_SR,
                canonical_slot=1,
                data={"claiming": "canonical too"},
            )

    def test_two_png_renders_on_one_revision_are_legal_when_they_differ_by_variant(self):
        """The panoramic bakes a MIP *and* a ray-sum strip from one arch."""
        annotation_set = self._set(kind="panoramic_arch")
        revision = self._revision(annotation_set)

        for variant in ("mip", "raysum"):
            AnnotationPayload.objects.create(
                revision=revision,
                format=PayloadFormat.PNG_RENDER,
                variant=variant,
                data={"strip": variant},
            )

        self.assertEqual(revision.payloads.filter(format=PayloadFormat.PNG_RENDER).count(), 2)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AnnotationPayload.objects.create(
                revision=revision, format=PayloadFormat.PNG_RENDER, variant="mip", data={}
            )


class PayloadBodyTests(AnnotationSchemaTestCase):
    """A payload holds inline JSON or points at an artifact -- exactly one."""

    def test_a_payload_with_neither_body_is_refused(self):
        revision = self._revision(self._set())

        with self.assertRaises(IntegrityError), transaction.atomic():
            AnnotationPayload.objects.create(
                revision=revision, format=PayloadFormat.NIFTI_LABELMAP
            )

    def test_a_payload_claiming_both_bodies_is_refused(self):
        from common.models import FileRegistry

        revision = self._revision(self._set())
        artifact = FileRegistry.objects.create(
            file_type="cbct_processed",
            file_path="maxillo/processed/labelmap.nii.gz",
            file_size=1,
            file_hash="0" * 64,
            domain="maxillo",
            patient=self.patient,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            AnnotationPayload.objects.create(
                revision=revision,
                format=PayloadFormat.NIFTI_LABELMAP,
                file=artifact,
                data={"and": "inline too"},
            )


class MeasurementCalibrationTests(AnnotationSchemaTestCase):
    """An uncalibrated number must not be reported in millimetres."""

    def setUp(self):
        self.annotation_set = self._set(kind="measurements")
        self.resource = self._resource("file:99", kind=ResourceKind.FILE)
        self.target = self._target(self.annotation_set, self.resource, role="image")
        self.revision = self._revision(self.annotation_set)
        self.geometry = Geometry2DItem.objects.create(
            revision=self.revision,
            target=self.target,
            geometry_type=Geometry2DType.POLYLINE,
            coordinate_system=CoordinateSystem.IMAGE_PIXEL,
            points=[[0, 0], [30, 40]],
        )

    def _measure(self, **kwargs):
        return MeasurementItem.objects.create(
            revision=self.revision,
            target=self.target,
            geometry_2d_item=self.geometry,
            kind=MeasurementKind.LENGTH,
            **kwargs,
        )

    def test_millimetres_without_calibration_are_refused_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._measure(value=50.0, unit=MeasurementUnit.MM, is_calibrated=False)

    def test_the_same_length_in_pixels_is_accepted(self):
        measurement = self._measure(
            value=50.0, unit=MeasurementUnit.PX, is_calibrated=False
        )

        self.assertEqual(measurement.unit, MeasurementUnit.PX)

    def test_millimetres_with_calibration_are_accepted(self):
        measurement = self._measure(
            value=12.5,
            unit=MeasurementUnit.MM,
            is_calibrated=True,
            calibration_note="DICOM PixelSpacing",
        )

        self.assertTrue(measurement.is_calibrated)

    def test_an_angle_is_calibration_free(self):
        """Degrees make no claim about physical size, so the rule does not apply."""
        measurement = MeasurementItem.objects.create(
            revision=self.revision,
            target=self.target,
            kind=MeasurementKind.ANGLE,
            value=37.2,
            unit=MeasurementUnit.DEG,
            is_calibrated=False,
        )

        self.assertEqual(measurement.unit, MeasurementUnit.DEG)

    def test_a_statistic_needs_no_geometry_at_all(self):
        measurement = MeasurementItem.objects.create(
            revision=self.revision,
            target=self.target,
            kind=MeasurementKind.MEAN,
            value=-412.0,
            unit=MeasurementUnit.HU,
            is_calibrated=True,
            sample_count=8192,
        )

        self.assertIsNone(measurement.geometry_2d_item_id)
        self.assertIsNone(measurement.spatial_3d_item_id)


class TimeSpanTests(AnnotationSchemaTestCase):
    """Intervals are half-open and ordered, in integer milliseconds."""

    def setUp(self):
        self.annotation_set = self._set(kind="video_regions")
        self.resource = self._resource("file:1200", kind=ResourceKind.FILE)
        self.target = self._target(self.annotation_set, self.resource, role="video")
        self.revision = self._revision(self.annotation_set)

    def test_an_end_before_its_start_is_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            TemporalAnnotationItem.objects.create(
                revision=self.revision,
                target=self.target,
                start_time_ms=5000,
                end_time_ms=4999,
            )

    def test_an_instant_is_a_zero_length_span(self):
        instant = TemporalAnnotationItem.objects.create(
            revision=self.revision,
            target=self.target,
            start_time_ms=5000,
            end_time_ms=5000,
        )

        self.assertEqual(instant.start_time_ms, instant.end_time_ms)

    def test_a_selector_with_a_reversed_span_is_refused_too(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            AnnotationSelector.objects.create(
                target=self.target,
                kind=SelectorKind.TEMPORAL_INTERVAL,
                coordinate_system=CoordinateSystem.VIDEO_PIXEL,
                start_time_ms=10_000,
                end_time_ms=1,
            )

    def test_a_selector_may_leave_the_span_open_ended(self):
        selector = AnnotationSelector.objects.create(
            target=self.target,
            kind=SelectorKind.FRAME,
            coordinate_system=CoordinateSystem.VIDEO_PIXEL,
            frame_index=42,
        )

        self.assertIsNone(selector.start_time_ms)


class LabelVocabularyTests(AnnotationSchemaTestCase):
    """A label value's meaning is frozen for the life of the labelmaps using it."""

    def setUp(self):
        self.schema = LabelSchema.objects.create(
            name="Jaw structures", slug="jaw-structures", version=1
        )

    def test_a_value_cannot_be_reused_inside_one_schema(self):
        LabelDefinition.objects.create(
            schema=self.schema, value=2, display_name="Mandible"
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            LabelDefinition.objects.create(
                schema=self.schema, value=2, display_name="Maxilla"
            )

    def test_the_next_schema_version_may_reuse_the_value_with_a_new_meaning(self):
        """Which is the whole point of versioning instead of editing."""
        LabelDefinition.objects.create(
            schema=self.schema, value=2, display_name="Mandible"
        )
        v2 = LabelSchema.objects.create(
            name="Jaw structures", slug="jaw-structures", version=2
        )

        LabelDefinition.objects.create(schema=v2, value=2, display_name="Maxilla")

        self.assertEqual(
            LabelDefinition.objects.filter(
                value=2, schema__slug="jaw-structures"
            ).count(),
            2,
        )

    def test_codes_are_unique_where_present_and_absent_ones_do_not_collide(self):
        LabelDefinition.objects.create(
            schema=self.schema, value=11, code="11", display_name="Upper right central"
        )
        # Two unlabelled definitions: NULL codes stay distinct, which is why the
        # column is nullable rather than blank.
        LabelDefinition.objects.create(schema=self.schema, value=90, display_name="Air")
        LabelDefinition.objects.create(schema=self.schema, value=91, display_name="Metal")

        with self.assertRaises(IntegrityError), transaction.atomic():
            LabelDefinition.objects.create(
                schema=self.schema, value=21, code="11", display_name="Duplicate FDI"
            )


class SourceResourceIdentityTests(AnnotationSchemaTestCase):
    """One resource per identity, unconditionally."""

    def test_the_identity_key_is_unique_across_every_kind(self):
        self._resource("file:7", kind=ResourceKind.FILE)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._resource("file:7", kind=ResourceKind.DERIVED_RESOURCE)

    def test_a_file_and_the_logical_volume_inside_it_are_separate_resources(self):
        from annotations import identity

        file_resource = self._resource(identity.for_file(412), kind=ResourceKind.FILE)
        volume_resource = self._resource(
            identity.for_logical_volume(412, "volume_nifti")
        )

        self.assertNotEqual(file_resource.identity_key, volume_resource.identity_key)

    def test_the_file_a_resource_points_at_cannot_be_deleted_out_from_under_it(self):
        from django.db.models import ProtectedError

        from common.models import FileRegistry

        artifact = FileRegistry.objects.create(
            file_type="cbct_raw",
            file_path="maxillo/raw/protected.nii.gz",
            file_size=1,
            file_hash="0" * 64,
            domain="maxillo",
            patient=self.patient,
        )
        SourceResource.objects.create(
            kind=ResourceKind.FILE,
            identity_key=f"file:{artifact.pk}",
            file=artifact,
        )

        with self.assertRaises(ProtectedError):
            artifact.delete()
