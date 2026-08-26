"""The write path: concurrency, the monotonic flag, and the cross-checks.

Three things are worth proving here and the rest follows from them.

* A stale writer loses, and loses *cleanly* -- with a 409-shaped exception and a
  transaction the caller can still roll back, not a poisoned atomic block.
* ``ever_annotated`` only ever turns on. Decision #18 removed the escape hatch
  where deleting the work unfroze the scan, and a flag that could be cleared
  would put it back without anyone noticing.
* The relationships a pure validator cannot see are checked anyway: an item
  cannot borrow another set's target, another target's selector, or another
  schema's label.
"""

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase

from annotations import services
from annotations.constants import (
    AnnotationOrigin,
    CoordinateSystem,
    Geometry2DType,
    Geometry3DType,
    MeasurementKind,
    MeasurementUnit,
    PayloadFormat,
    ResourceKind,
    SelectorKind,
    SliceAxis,
)
from annotations.models import (
    AnnotationSelector,
    LabelDefinition,
    LabelSchema,
    SourceResource,
)
from annotations.services.exceptions import AnnotationConflict, AnnotationNotAllowed
from common.models import AnnotationMethod, FileRegistry, Project
from maxillo.models import Folder, Patient


class ServiceTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project, _ = Project.objects.update_or_create(
            slug="maxillo", defaults={"name": "maxillo", "domain": "maxillo"}
        )
        cls.user = User.objects.create_user(username="service-user", password="x")
        cls.folder = Folder.objects.create(name="Services", project=cls.project)
        cls.patient = Patient.objects.create(
            name="Serviced", folder=cls.folder, project=cls.project
        )

    def _file(self, path, file_type="cbct_processed"):
        return FileRegistry.objects.create(
            file_type=file_type,
            file_path=path,
            file_size=1,
            file_hash="0" * 64,
            domain="maxillo",
            patient=self.patient,
        )

    def _anchored_set(self, kind="volume_segmentation", **kwargs):
        annotation_set = services.get_or_create_set(
            self.patient, kind, created_by=self.user, **kwargs
        )
        resource = services.register_logical_volume(
            self._file(f"maxillo/processed/{kind}-{annotation_set.pk}.nii.gz"),
            file_key="volume_nifti",
            content_hash="a" * 64,
        )
        target = services.attach_target(
            annotation_set, resource, role="volume", primary=True
        )
        return annotation_set, target


class ResourceRegistrationTests(ServiceTestCase):
    def test_registering_the_same_member_twice_yields_one_resource(self):
        artifact = self._file("maxillo/processed/bundle.json")

        first = services.register_logical_volume(artifact, file_key="volume_nifti")
        second = services.register_logical_volume(artifact, file_key="volume_nifti")

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SourceResource.objects.count(), 1)

    def test_two_members_of_one_bundle_are_two_resources(self):
        artifact = self._file("maxillo/processed/bundle.json")

        volume = services.register_logical_volume(artifact, file_key="volume_nifti")
        segmentation = services.register_logical_volume(
            artifact, file_key="segmentation_nifti"
        )

        self.assertNotEqual(volume.pk, segmentation.pk)

    def test_re_registering_refreshes_the_hash_without_moving_the_identity(self):
        artifact = self._file("maxillo/processed/bundle.json")
        first = services.register_logical_volume(
            artifact, file_key="volume_nifti", content_hash="a" * 64
        )

        second = services.register_logical_volume(
            artifact, file_key="volume_nifti", content_hash="b" * 64
        )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.content_hash, "b" * 64)
        self.assertEqual(second.identity_key, first.identity_key)

    def test_a_derived_resource_is_distinct_per_source(self):
        one = services.register_logical_volume(self._file("maxillo/a.nii.gz"))
        two = services.register_logical_volume(self._file("maxillo/b.nii.gz"))

        first = services.register_derived("panorex-js-v2", one, discriminator="mip")
        second = services.register_derived("panorex-js-v2", two, discriminator="mip")

        self.assertNotEqual(first.identity_key, second.identity_key)


class SetCreationTests(ServiceTestCase):
    def test_the_project_gate_is_part_of_the_write_path(self):
        """F11's lesson: a gate every caller must remember is a gate that fails."""
        method = AnnotationMethod.objects.get(slug="ios_landmarks")
        self.project.annotation_methods.set([])

        with self.assertRaises(AnnotationNotAllowed):
            services.get_or_create_set(
                self.patient, "ios_landmarks", annotation_method=method
            )

    def test_an_enabled_method_passes(self):
        method = AnnotationMethod.objects.get(slug="ios_landmarks")
        self.project.annotation_methods.set([method])

        annotation_set = services.get_or_create_set(
            self.patient, "ios_landmarks", annotation_method=method
        )

        self.assertEqual(annotation_set.annotation_method_id, method.pk)

    def test_a_migration_may_opt_out_of_the_gate(self):
        """Legacy work predates the registry; dropping it would lose real data."""
        method = AnnotationMethod.objects.get(slug="ios_landmarks")
        self.project.annotation_methods.set([])

        annotation_set = services.get_or_create_set(
            self.patient,
            "ios_landmarks",
            annotation_method=method,
            check_project=False,
        )

        self.assertIsNotNone(annotation_set.pk)

    def test_the_patient_lands_in_the_right_domain_column(self):
        annotation_set = services.get_or_create_set(self.patient, "measurements")

        self.assertEqual(annotation_set.domain, "maxillo")
        self.assertEqual(annotation_set.patient_id, self.patient.pk)
        self.assertIsNone(annotation_set.brain_patient_id)
        self.assertIsNone(annotation_set.laparoscopy_patient_id)
        self.assertEqual(annotation_set.get_patient(), self.patient)

    def test_a_second_call_returns_the_same_set(self):
        first = services.get_or_create_set(self.patient, "measurements")
        second = services.get_or_create_set(self.patient, "measurements")

        self.assertEqual(first.pk, second.pk)


class TargetTests(ServiceTestCase):
    def test_making_a_second_target_primary_moves_the_slot(self):
        annotation_set, first = self._anchored_set()
        other = services.register_logical_volume(self._file("maxillo/other.nii.gz"))

        second = services.attach_target(
            annotation_set, other, role="segmentation", primary=True
        )

        first.refresh_from_db()
        self.assertIsNone(first.primary_slot)
        self.assertEqual(second.primary_slot, 1)


class RevisionConcurrencyTests(ServiceTestCase):
    def test_a_stale_writer_gets_a_conflict(self):
        annotation_set, _ = self._anchored_set()
        services.record_revision(annotation_set, expected_revision=0, author=self.user)

        # Both clients loaded revision 1.
        services.record_revision(annotation_set, expected_revision=1, author=self.user)
        with self.assertRaises(AnnotationConflict) as caught:
            services.record_revision(
                annotation_set, expected_revision=1, author=self.user
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(annotation_set.revisions.count(), 2)

    def test_a_conflict_leaves_the_transaction_usable(self):
        """The savepoint matters: a caller has to be able to report the 409."""
        annotation_set, _ = self._anchored_set()
        services.record_revision(annotation_set, expected_revision=0, author=self.user)

        with transaction.atomic():
            with self.assertRaises(AnnotationConflict):
                services.record_revision(
                    annotation_set, expected_revision=0, author=self.user
                )
            # Still able to read and write after the failed insert.
            self.assertEqual(services.current_revision_number(annotation_set), 1)

    def test_omitting_the_expectation_appends(self):
        annotation_set, _ = self._anchored_set()
        services.record_revision(annotation_set, author=self.user)
        second = services.record_revision(annotation_set, author=self.user)

        self.assertEqual(second.revision_number, 2)

    def test_the_revision_fingerprints_its_targets(self):
        annotation_set, target = self._anchored_set()

        revision = services.record_revision(annotation_set, author=self.user)

        self.assertEqual(
            revision.source_fingerprint,
            {target.source_resource.identity_key: "a" * 64},
        )


class EverAnnotatedTests(ServiceTestCase):
    def test_human_work_sets_the_flag(self):
        annotation_set, _ = self._anchored_set()
        self.assertFalse(annotation_set.ever_annotated)

        services.record_revision(
            annotation_set, author=self.user, origin=AnnotationOrigin.MANUAL
        )

        annotation_set.refresh_from_db()
        self.assertTrue(annotation_set.ever_annotated)

    def test_a_prediction_alone_does_not(self):
        """Machine output has never locked a patient's raw data, and still does not."""
        annotation_set, _ = self._anchored_set()

        services.record_revision(
            annotation_set, author=None, origin=AnnotationOrigin.PREDICTION
        )

        annotation_set.refresh_from_db()
        self.assertFalse(annotation_set.ever_annotated)

    def test_a_later_prediction_cannot_clear_it(self):
        annotation_set, _ = self._anchored_set()
        services.record_revision(annotation_set, origin=AnnotationOrigin.MANUAL)

        services.record_revision(annotation_set, origin=AnnotationOrigin.PREDICTION)

        annotation_set.refresh_from_db()
        self.assertTrue(annotation_set.ever_annotated)

    def test_a_migrated_revision_counts_as_human_work(self):
        """It records work a person did; only the mechanism was different."""
        annotation_set, _ = self._anchored_set()

        services.record_revision(annotation_set, origin=AnnotationOrigin.MIGRATION)

        annotation_set.refresh_from_db()
        self.assertTrue(annotation_set.ever_annotated)


class PayloadTests(ServiceTestCase):
    def test_viewer_state_can_never_be_canonical(self):
        annotation_set, _ = self._anchored_set()
        revision = services.record_revision(annotation_set)

        with self.assertRaises(ValueError):
            services.add_payload(
                revision,
                format=PayloadFormat.CORNERSTONE_STATE,
                data={"annotations": []},
                canonical=True,
            )

    def test_viewer_state_alongside_a_canonical_document_is_fine(self):
        annotation_set, _ = self._anchored_set()
        revision = services.record_revision(annotation_set)

        services.add_payload(
            revision, format=PayloadFormat.YGGDRASIL_JSON, data={"items": []}, canonical=True
        )
        services.add_payload(
            revision, format=PayloadFormat.CORNERSTONE_STATE, data={"annotations": []}
        )

        self.assertEqual(revision.payloads.count(), 2)
        self.assertEqual(revision.payloads.filter(canonical_slot=1).count(), 1)


class ItemMembershipTests(ServiceTestCase):
    def setUp(self):
        self.annotation_set, self.target = self._anchored_set()
        self.revision = services.record_revision(self.annotation_set)

    def test_an_item_cannot_borrow_another_sets_target(self):
        other_set, other_target = self._anchored_set(kind="measurements")

        with self.assertRaises(ValidationError):
            services.add_geometry_2d(
                self.revision,
                other_target,
                geometry_type=Geometry2DType.POINT,
                coordinate_system=CoordinateSystem.IMAGE_PIXEL,
                points=[[1, 1]],
            )

    def test_an_item_cannot_borrow_another_targets_selector(self):
        other = services.register_logical_volume(self._file("maxillo/second.nii.gz"))
        other_target = services.attach_target(self.annotation_set, other, role="second")
        foreign_selector = AnnotationSelector.objects.create(
            target=other_target,
            kind=SelectorKind.SLICE,
            coordinate_system=CoordinateSystem.SLICE_PIXEL,
            slice_axis=SliceAxis.AXIAL,
            slice_index=100,
        )

        with self.assertRaises(ValidationError):
            services.add_geometry_2d(
                self.revision,
                self.target,
                selector=foreign_selector,
                geometry_type=Geometry2DType.POINT,
                coordinate_system=CoordinateSystem.SLICE_PIXEL,
                points=[[1, 1]],
            )

    def test_a_label_from_another_schema_is_refused(self):
        used = LabelSchema.objects.create(name="Used", slug="used")
        other = LabelSchema.objects.create(name="Other", slug="other")
        self.annotation_set.label_schema = used
        self.annotation_set.save(update_fields=["label_schema"])
        self.revision.refresh_from_db()
        foreign = LabelDefinition.objects.create(
            schema=other, value=2, display_name="Mandible elsewhere"
        )

        with self.assertRaises(ValidationError) as caught:
            services.add_geometry_2d(
                self.revision,
                self.target,
                label=foreign,
                geometry_type=Geometry2DType.POINT,
                coordinate_system=CoordinateSystem.IMAGE_PIXEL,
                points=[[1, 1]],
            )

        self.assertIn("mean something else", str(caught.exception))

    def test_a_set_with_no_schema_cannot_carry_labels_at_all(self):
        schema = LabelSchema.objects.create(name="Loose", slug="loose")
        label = LabelDefinition.objects.create(
            schema=schema, value=1, display_name="Anything"
        )

        with self.assertRaises(ValidationError):
            services.add_geometry_2d(
                self.revision,
                self.target,
                label=label,
                geometry_type=Geometry2DType.POINT,
                coordinate_system=CoordinateSystem.IMAGE_PIXEL,
                points=[[1, 1]],
            )

    def test_a_slice_spline_needs_its_slice_selector(self):
        with self.assertRaises(ValidationError):
            services.add_geometry_2d(
                self.revision,
                self.target,
                geometry_type=Geometry2DType.POLYLINE,
                coordinate_system=CoordinateSystem.SLICE_PIXEL,
                points=[[0, 0], [10, 10]],
            )

        selector = AnnotationSelector.objects.create(
            target=self.target,
            kind=SelectorKind.SLICE,
            coordinate_system=CoordinateSystem.SLICE_PIXEL,
            slice_axis=SliceAxis.AXIAL,
            slice_index=128,
        )
        spline = services.add_geometry_2d(
            self.revision,
            self.target,
            selector=selector,
            geometry_type=Geometry2DType.POLYLINE,
            coordinate_system=CoordinateSystem.SLICE_PIXEL,
            points=[[0, 0], [10, 10]],
        )

        self.assertEqual(spline.selector_id, selector.pk)


class MeasurementServiceTests(ServiceTestCase):
    def setUp(self):
        self.annotation_set, self.target = self._anchored_set(kind="measurements")
        self.revision = services.record_revision(self.annotation_set)
        self.shape = services.add_geometry_2d(
            self.revision,
            self.target,
            geometry_type=Geometry2DType.POLYLINE,
            coordinate_system=CoordinateSystem.IMAGE_PIXEL,
            points=[[0, 0], [30, 40]],
        )

    def test_naming_two_shapes_is_ambiguous_and_refused(self):
        solid = services.add_spatial_3d(
            self.revision,
            self.target,
            geometry_type=Geometry3DType.POINT,
            coordinate_system=CoordinateSystem.PATIENT_LPS_MM,
            points=[[1, 2, 3]],
        )

        with self.assertRaises(ValidationError):
            services.add_measurement(
                self.revision,
                self.target,
                kind=MeasurementKind.LENGTH,
                value=50,
                unit=MeasurementUnit.PX,
                geometry_2d_item=self.shape,
                spatial_3d_item=solid,
            )

    def test_a_shape_from_another_revision_is_refused(self):
        later = services.record_revision(self.annotation_set)

        with self.assertRaises(ValidationError):
            services.add_measurement(
                later,
                self.target,
                kind=MeasurementKind.LENGTH,
                value=50,
                unit=MeasurementUnit.PX,
                geometry_2d_item=self.shape,
            )

    def test_an_uncalibrated_length_is_stored_in_pixels(self):
        measurement = services.add_measurement(
            self.revision,
            self.target,
            kind=MeasurementKind.LENGTH,
            value=50,
            unit=MeasurementUnit.PX,
            geometry_2d_item=self.shape,
        )

        self.assertFalse(measurement.is_calibrated)
        self.assertEqual(measurement.unit, MeasurementUnit.PX)


class TemporalServiceTests(ServiceTestCase):
    def setUp(self):
        self.annotation_set, self.target = self._anchored_set(kind="video_regions")
        self.revision = services.record_revision(self.annotation_set)

    def test_float_seconds_are_refused_with_the_reason(self):
        """The legacy laparoscopy column is float seconds; this is the boundary."""
        with self.assertRaises(ValidationError) as caught:
            services.add_temporal(
                self.revision, self.target, start_time_ms=1.5, end_time_ms=2000
            )

        self.assertIn("milliseconds", str(caught.exception))

    def test_an_instant_is_allowed(self):
        instant = services.add_temporal(
            self.revision, self.target, start_time_ms=4000, end_time_ms=4000
        )

        self.assertEqual(instant.start_time_ms, 4000)


class EventServiceTests(ServiceTestCase):
    def setUp(self):
        self.annotation_set, self.target = self._anchored_set(
            kind="occlusion_classification"
        )
        self.revision = services.record_revision(self.annotation_set)

    def test_an_event_asserting_nothing_is_refused(self):
        with self.assertRaises(ValidationError):
            services.add_event(
                self.revision, self.target, event_type="occlusion.vertical"
            )

    def test_a_free_text_value_is_accepted_where_no_schema_covers_it(self):
        event = services.add_event(
            self.revision,
            self.target,
            event_type="occlusion.vertical",
            value="Normal",
        )

        self.assertEqual(event.value, "Normal")

    def test_five_facets_are_five_rows(self):
        for facet in (
            "sagittal_left",
            "sagittal_right",
            "vertical",
            "transverse",
            "midline",
        ):
            services.add_event(
                self.revision,
                self.target,
                event_type=f"occlusion.{facet}",
                value="Unknown",
            )

        self.assertEqual(self.revision.eventannotationitems.count(), 5)


class FingerprintTests(ServiceTestCase):
    def test_a_resource_with_no_known_hash_is_recorded_as_unknown_not_omitted(self):
        annotation_set = services.get_or_create_set(self.patient, "measurements")
        resource = services.register_logical_volume(self._file("maxillo/unhashed.nii.gz"))
        services.attach_target(annotation_set, resource, role="volume", primary=True)

        revision = services.record_revision(annotation_set)

        self.assertEqual(revision.source_fingerprint, {resource.identity_key: ""})


class ResourceKindTests(ServiceTestCase):
    def test_a_file_resource_and_a_volume_resource_coexist_for_one_row(self):
        artifact = self._file("maxillo/processed/both.nii.gz")

        as_file = services.register_file(artifact)
        as_volume = services.register_logical_volume(artifact)

        self.assertEqual(as_file.kind, ResourceKind.FILE)
        self.assertEqual(as_volume.kind, ResourceKind.LOGICAL_VOLUME)
        self.assertNotEqual(as_file.pk, as_volume.pk)
