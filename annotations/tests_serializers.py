"""The canonical document.

Two properties carry the weight here. The first is that no Cornerstone runtime
identifier can reach it -- ``annotationUID``, ``imageId``, ``cachedStats`` are
session-scoped, and a document carrying one looks durable while not being. The
second is that every item states its own coordinate frame rather than inheriting
one, because inheritance is how an item that moves to a different resource keeps
a frame that no longer applies, silently.
"""

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase

from annotations import serializers, services
from annotations.adapters import legacy_maxillo
from annotations.constants import (
    AnnotationOrigin,
    CoordinateSystem,
    Geometry2DType,
    Geometry3DType,
    MeasurementKind,
    MeasurementUnit,
    PayloadFormat,
)
from annotations.models import LabelDefinition, LabelSchema
from common.models import FileRegistry, Project
from maxillo.models import Folder, Patient


class DocumentTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project, _ = Project.objects.update_or_create(
            slug="maxillo", defaults={"name": "maxillo", "domain": "maxillo"}
        )
        cls.user = User.objects.create_user(username="doc-user", password="x")
        cls.folder = Folder.objects.create(name="Docs", project=cls.project)
        cls.patient = Patient.objects.create(
            name="Documented", folder=cls.folder, project=cls.project
        )

    def _file(self, path):
        return FileRegistry.objects.create(
            file_type="cbct_processed",
            file_path=path,
            file_size=1,
            file_hash="0" * 64,
            domain="maxillo",
            patient=self.patient,
        )

    def _prepared(self, kind="measurements", label_schema=None):
        annotation_set = services.get_or_create_set(
            self.patient, kind, label_schema=label_schema, created_by=self.user
        )
        resource = services.register_logical_volume(
            self._file(f"maxillo/processed/{kind}.nii.gz"),
            file_key="volume_nifti",
            content_hash="a" * 64,
            descriptor={"shape": [400, 400, 300]},
        )
        target = services.attach_target(
            annotation_set, resource, role="volume", primary=True
        )
        revision = services.record_revision(annotation_set, author=self.user)
        return annotation_set, target, revision


class ShapeTests(DocumentTestCase):
    def test_the_document_names_its_version(self):
        _, _, revision = self._prepared()

        document = serializers.build_document(revision)

        self.assertEqual(document["document_version"], serializers.DOCUMENT_VERSION)

    def test_every_item_carries_its_own_coordinate_frame(self):
        """Never inherited: an item that moves keeps a frame it no longer has."""
        _, target, revision = self._prepared()
        services.add_geometry_2d(
            revision,
            target,
            geometry_type=Geometry2DType.POINT,
            coordinate_system=CoordinateSystem.IMAGE_PIXEL,
            points=[[1, 2]],
        )
        services.add_spatial_3d(
            revision,
            target,
            geometry_type=Geometry3DType.POINT,
            coordinate_system=CoordinateSystem.PATIENT_LPS_MM,
            points=[[1, 2, 3]],
        )

        document = serializers.build_document(revision)

        self.assertEqual(
            document["geometry_2d"][0]["coordinate_system"], CoordinateSystem.IMAGE_PIXEL
        )
        self.assertEqual(
            document["spatial_3d"][0]["coordinate_system"], CoordinateSystem.PATIENT_LPS_MM
        )

    def test_targets_carry_their_identity_hash_and_descriptor(self):
        _, _, revision = self._prepared()

        document = serializers.build_document(revision)

        target = document["targets"][0]
        self.assertTrue(target["primary"])
        self.assertEqual(target["content_hash"], "a" * 64)
        self.assertEqual(target["descriptor"]["shape"], [400, 400, 300])
        self.assertTrue(target["identity_key"].startswith("logical_volume:"))

    def test_the_revision_carries_the_fingerprint_that_makes_it_checkable(self):
        _, target, revision = self._prepared()

        document = serializers.build_document(revision)

        self.assertEqual(
            document["revision"]["source_fingerprint"],
            {target.source_resource.identity_key: "a" * 64},
        )

    def test_a_label_is_emitted_with_both_its_value_and_its_code(self):
        """The value is what a labelmap holds; the code is what imports match on."""
        schema = LabelSchema.objects.create(name="Teeth", slug="doc-teeth")
        label = LabelDefinition.objects.create(
            schema=schema, value=7, code="11", display_name="Upper right central"
        )
        _, target, revision = self._prepared(
            kind="intraoral_segmentation", label_schema=schema
        )
        services.add_geometry_2d(
            revision,
            target,
            label=label,
            geometry_type=Geometry2DType.POLYGON,
            coordinate_system=CoordinateSystem.IMAGE_PIXEL,
            points=[[0, 0], [1, 0], [1, 1]],
            closed=True,
        )

        emitted = serializers.build_document(revision)["geometry_2d"][0]["label"]

        self.assertEqual(emitted["value"], 7)
        self.assertEqual(emitted["code"], "11")
        self.assertEqual(emitted["schema"], "doc-teeth")
        self.assertEqual(emitted["schema_version"], 1)

    def test_calibration_is_always_stated_never_inferred(self):
        _, target, revision = self._prepared()
        services.add_measurement(
            revision,
            target,
            kind=MeasurementKind.LENGTH,
            value=50,
            unit=MeasurementUnit.PX,
            is_calibrated=False,
        )

        emitted = serializers.build_document(revision)["measurements"][0]

        self.assertIn("is_calibrated", emitted)
        self.assertFalse(emitted["is_calibrated"])
        self.assertEqual(emitted["unit"], MeasurementUnit.PX)

    def test_a_slice_selector_survives_into_the_document(self):
        annotation_set = services.get_or_create_set(self.patient, "panoramic_arch")
        resource = services.register_logical_volume(self._file("maxillo/arch.nii.gz"))
        target = services.attach_target(annotation_set, resource, role="volume", primary=True)
        revision = services.record_revision(annotation_set)
        services.apply_descriptors(
            revision,
            target,
            legacy_maxillo.panoramic_arch(
                [[0, 0], [1, 1], [2, 2], [3, 3]],
                axial_slice=128,
                volume_shape=[400, 400, 300],
                geometry_source="custom_cp",
                default_mode="mip",
            ),
            require_labels=False,
        )

        emitted = serializers.build_document(revision)["geometry_2d"][0]["selector"]

        self.assertEqual(emitted["slice_axis"], "axial")
        self.assertEqual(emitted["slice_index"], 128)

    def test_an_item_with_no_target_emits_null_rather_than_being_dropped(self):
        annotation_set = services.get_or_create_set(
            self.patient, "occlusion_classification"
        )
        revision = services.record_revision(annotation_set)
        services.add_event(revision, event_type="occlusion.vertical", value="Normal")

        document = serializers.build_document(revision)

        self.assertEqual(len(document["events"]), 1)
        self.assertIsNone(document["events"][0]["target"])


class ViewerIdentifierTests(DocumentTestCase):
    def test_a_built_document_is_clean(self):
        _, target, revision = self._prepared()
        services.add_geometry_2d(
            revision,
            target,
            geometry_type=Geometry2DType.POINT,
            coordinate_system=CoordinateSystem.IMAGE_PIXEL,
            points=[[1, 2]],
        )

        serializers.assert_no_viewer_identifiers(serializers.build_document(revision))

    def test_a_uid_smuggled_in_through_attributes_is_caught(self):
        """The realistic route: somebody stores the tool payload wholesale."""
        _, target, revision = self._prepared()
        services.add_geometry_2d(
            revision,
            target,
            geometry_type=Geometry2DType.POINT,
            coordinate_system=CoordinateSystem.IMAGE_PIXEL,
            points=[[1, 2]],
            attributes={"annotationUID": "a3f1-…-9c2"},
        )

        with self.assertRaises(ValidationError) as caught:
            serializers.assert_no_viewer_identifiers(
                serializers.build_document(revision)
            )

        self.assertIn("annotationUID", str(caught.exception))

    def test_cached_stats_are_caught_too(self):
        for key in ("cachedStats", "imageId", "volumeId", "toolName"):
            with self.subTest(key=key):
                with self.assertRaises(ValidationError):
                    serializers.assert_no_viewer_identifiers({"a": [{key: "x"}]})

    def test_the_check_reaches_arbitrary_nesting(self):
        with self.assertRaises(ValidationError):
            serializers.assert_no_viewer_identifiers(
                {"items": [{"attributes": {"nested": {"imageId": "wadors:..."}}}]}
            )


class PayloadIntegrationTests(DocumentTestCase):
    def test_a_document_can_be_stored_as_the_canonical_payload(self):
        _, target, revision = self._prepared()
        services.add_measurement(
            revision,
            target,
            kind=MeasurementKind.LENGTH,
            value=50,
            unit=MeasurementUnit.PX,
        )
        document = serializers.assert_no_viewer_identifiers(
            serializers.build_document(revision)
        )

        payload = services.add_payload(
            revision,
            format=PayloadFormat.YGGDRASIL_JSON,
            data=document,
            canonical=True,
        )

        payload.refresh_from_db()
        self.assertEqual(payload.canonical_slot, 1)
        self.assertEqual(payload.data["measurements"][0]["value"], 50.0)

    def test_viewer_state_sits_beside_it_and_is_never_canonical(self):
        _, _, revision = self._prepared()
        services.add_payload(
            revision,
            format=PayloadFormat.YGGDRASIL_JSON,
            data=serializers.build_document(revision),
            canonical=True,
        )

        # Full of runtime ids, which is fine: it is scratch state, not truth.
        services.add_payload(
            revision,
            format=PayloadFormat.CORNERSTONE_STATE,
            data={"annotations": [{"annotationUID": "a3f1", "cachedStats": {}}]},
        )

        self.assertEqual(revision.payloads.filter(canonical_slot=1).count(), 1)
        self.assertEqual(
            revision.payloads.get(canonical_slot=1).format, PayloadFormat.YGGDRASIL_JSON
        )


class SummaryTests(DocumentTestCase):
    def test_the_summary_counts_every_section(self):
        _, target, revision = self._prepared()
        services.add_geometry_2d(
            revision,
            target,
            geometry_type=Geometry2DType.POINT,
            coordinate_system=CoordinateSystem.IMAGE_PIXEL,
            points=[[1, 2]],
        )
        services.add_event(revision, target, event_type="note", value="x")

        summary = serializers.document_summary(serializers.build_document(revision))

        self.assertEqual(summary["geometry_2d"], 1)
        self.assertEqual(summary["events"], 1)
        self.assertEqual(summary["measurements"], 0)
