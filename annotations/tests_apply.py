"""Adapters through the applier into the database: the conversion end to end.

The pure adapter tests prove the translation is right and the service tests
prove the write path is right. This is the seam between them, and it has its own
failure modes: a label code that resolves to nothing, a selector recreated once
per descriptor, a descriptor kind nobody dispatches.

The last one is the reason ``apply_descriptors`` raises on an unknown kind
instead of skipping it. A conversion that silently drops a descriptor reports
success and loses data, and the place that surfaces is a patient's screen.
"""

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase

from annotations import services
from annotations.adapters import legacy_laparoscopy, legacy_maxillo
from annotations.constants import AnnotationOrigin, CoordinateSystem
from annotations.models import AnnotationSelector, LabelDefinition, LabelSchema
from common.models import FileRegistry, Project
from maxillo.models import Folder, Patient


class ApplyTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project, _ = Project.objects.update_or_create(
            slug="maxillo", defaults={"name": "maxillo", "domain": "maxillo"}
        )
        cls.user = User.objects.create_user(username="apply-user", password="x")
        cls.folder = Folder.objects.create(name="Apply", project=cls.project)
        cls.patient = Patient.objects.create(
            name="Applied", folder=cls.folder, project=cls.project
        )

    def _fdi_schema(self, *codes):
        schema = LabelSchema.objects.create(name="FDI", slug="fdi-permanent")
        for index, code in enumerate(codes, start=1):
            LabelDefinition.objects.create(
                schema=schema, value=index, code=code, display_name=f"Tooth {code}"
            )
        return schema

    def _file(self, path):
        return FileRegistry.objects.create(
            file_type="intraoral_processed",
            file_path=path,
            file_size=1,
            file_hash="0" * 64,
            domain="maxillo",
            patient=self.patient,
        )

    def _prepared(self, kind, *, label_schema=None, path="maxillo/intraoral/1.png"):
        annotation_set = services.get_or_create_set(
            self.patient, kind, label_schema=label_schema, created_by=self.user
        )
        resource = services.register_file(self._file(path))
        target = services.attach_target(
            annotation_set, resource, role="image", primary=True
        )
        revision = services.record_revision(
            annotation_set, author=self.user, origin=AnnotationOrigin.MIGRATION
        )
        return annotation_set, target, revision


class LabelResolutionTests(ApplyTestCase):
    def test_an_fdi_code_resolves_to_this_schemas_definition(self):
        schema = self._fdi_schema("11", "21")
        _, target, revision = self._prepared(
            "intraoral_segmentation", label_schema=schema
        )
        descriptors = legacy_maxillo.intraoral_segmentation(
            {"11": [[[0, 0], [10, 0], [10, 10]]]}
        )

        written = services.apply_descriptors(revision, target, descriptors)

        self.assertEqual(written[0].label.code, "11")
        self.assertEqual(written[0].label.schema_id, schema.pk)

    def test_an_unresolvable_code_is_refused_rather_than_written_unlabelled(self):
        """An unlabelled polygon exports under the wrong segment and looks fine."""
        schema = self._fdi_schema("11")
        _, target, revision = self._prepared(
            "intraoral_segmentation", label_schema=schema
        )
        descriptors = legacy_maxillo.intraoral_segmentation(
            {"48": [[[0, 0], [10, 0], [10, 10]]]}
        )

        with self.assertRaises(ValidationError) as caught:
            services.apply_descriptors(revision, target, descriptors)

        self.assertIn("48", str(caught.exception))

    def test_a_caller_may_opt_out_where_no_vocabulary_exists_yet(self):
        _, target, revision = self._prepared("intraoral_segmentation")
        descriptors = legacy_maxillo.intraoral_segmentation(
            {"48": [[[0, 0], [10, 0], [10, 10]]]}
        )

        written = services.apply_descriptors(
            revision, target, descriptors, require_labels=False
        )

        self.assertIsNone(written[0].label_id)


class SelectorReuseTests(ApplyTestCase):
    def test_one_selector_is_shared_by_everything_on_a_frame(self):
        """A row per descriptor would make "what is on this frame" a scan."""
        _, target, revision = self._prepared("video_regions")
        descriptors = legacy_laparoscopy.region_annotation(
            tool="brush",
            frame_time=2.0,
            points=[0, 0, 10, 10],
            prompt_points=[{"x": 0.25, "y": 0.5}, {"x": 0.75, "y": 0.5, "label": 0}],
        )

        written = services.apply_descriptors(
            revision, target, descriptors, require_labels=False
        )

        self.assertEqual(AnnotationSelector.objects.filter(target=target).count(), 1)
        self.assertEqual(len({item.selector_id for item in written}), 1)

    def test_two_frames_get_two_selectors(self):
        _, target, revision = self._prepared("video_regions")
        first = legacy_laparoscopy.region_annotation(
            tool="brush", frame_time=1.0, points=[0, 0, 1, 1]
        )
        second = legacy_laparoscopy.region_annotation(
            tool="brush", frame_time=2.0, points=[0, 0, 1, 1]
        )

        services.apply_descriptors(revision, target, first, require_labels=False)
        services.apply_descriptors(revision, target, second, require_labels=False)

        self.assertEqual(AnnotationSelector.objects.filter(target=target).count(), 2)

    def test_reapplying_the_same_frame_reuses_the_stored_selector(self):
        """The cache is per call; get_or_create is what makes it idempotent."""
        _, target, revision = self._prepared("video_regions")
        descriptors = legacy_laparoscopy.region_annotation(
            tool="brush", frame_time=1.0, points=[0, 0, 1, 1]
        )

        services.apply_descriptors(revision, target, descriptors, require_labels=False)
        services.apply_descriptors(revision, target, descriptors, require_labels=False)

        self.assertEqual(AnnotationSelector.objects.filter(target=target).count(), 1)


class DispatchTests(ApplyTestCase):
    def test_an_unknown_descriptor_kind_is_an_error_not_a_skip(self):
        _, target, revision = self._prepared("measurements")

        with self.assertRaises(ValidationError) as caught:
            services.apply_descriptors(revision, target, [{"item": "hologram"}])

        self.assertIn("hologram", str(caught.exception))

    def test_a_failure_partway_leaves_nothing_behind_inside_a_transaction(self):
        _, target, revision = self._prepared("measurements")
        good = legacy_maxillo.occlusion_classification({"vertical": "Normal"})

        with self.assertRaises(ValidationError):
            with transaction.atomic():
                services.apply_descriptors(
                    revision, target, [*good, {"item": "hologram"}]
                )

        self.assertEqual(revision.eventannotationitems.count(), 0)


class PanoramicRoundTripTests(ApplyTestCase):
    def test_the_arch_lands_with_a_real_slice_selector(self):
        _, target, revision = self._prepared("panoramic_arch")
        descriptors = legacy_maxillo.panoramic_arch(
            [[0, 0], [10, 5], [20, 5], [30, 0]],
            axial_slice=128,
            volume_shape=[400, 400, 300],
            geometry_source="custom_cp",
            default_mode="mip",
        )

        written = services.apply_descriptors(
            revision, target, descriptors, require_labels=False
        )

        spline = written[0]
        self.assertEqual(spline.coordinate_system, CoordinateSystem.SLICE_PIXEL)
        self.assertEqual(spline.selector.slice_index, 128)
        self.assertEqual(spline.selector.slice_axis, "axial")
        self.assertEqual(spline.points, [[0.0, 0.0], [10.0, 5.0], [20.0, 5.0], [30.0, 0.0]])

    def test_an_auto_arch_converts_without_locking_the_case(self):
        """It explains the baked strips; it is not human annotation work."""
        annotation_set = services.get_or_create_set(self.patient, "panoramic_arch")
        resource = services.register_file(self._file("maxillo/pano/auto.nii.gz"))
        target = services.attach_target(annotation_set, resource, role="volume", primary=True)
        revision = services.record_revision(
            annotation_set, origin=AnnotationOrigin.PREDICTION
        )

        services.apply_descriptors(
            revision,
            target,
            legacy_maxillo.panoramic_arch(
                [[0, 0], [1, 1], [2, 2], [3, 3]],
                axial_slice=10,
                volume_shape=[100, 100, 100],
                geometry_source="auto",
                default_mode="mip",
            ),
            require_labels=False,
        )

        annotation_set.refresh_from_db()
        self.assertFalse(annotation_set.ever_annotated)


class ClassificationRoundTripTests(ApplyTestCase):
    def test_a_classification_becomes_five_queryable_events(self):
        _, target, revision = self._prepared("occlusion_classification")

        services.apply_descriptors(
            revision,
            target,
            legacy_maxillo.occlusion_classification(
                {
                    "sagittal_left": "I",
                    "sagittal_right": "II",
                    "vertical": "Normal",
                    "transverse": "Unknown",
                    "midline": "Unknown",
                }
            ),
        )

        events = revision.eventannotationitems.order_by("order")
        self.assertEqual(events.count(), 5)
        self.assertEqual(
            events.get(event_type="occlusion.sagittal_right").value, "II"
        )
