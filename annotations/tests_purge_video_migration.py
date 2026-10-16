"""0010_purge_video_annotations against the shape production actually holds.

A video set's items point at its target with ``on_delete=PROTECT``. Deleting the set
reaches those items twice -- through the revision (CASCADE) and through the target
(PROTECT) -- and Django refuses the whole delete on the PROTECT. This failed ``migrate``
in production; the test plants the same shape and runs the migration's own function.
"""

import importlib
import uuid

from django.apps import apps
from django.test import TestCase

from annotations.constants import (
    CoordinateSystem,
    Geometry2DType,
    ResourceKind,
    SelectorKind,
)
from annotations.models import (
    AnnotationRevision,
    AnnotationSelector,
    AnnotationSet,
    AnnotationTarget,
    EventAnnotationItem,
    Geometry2DItem,
    LabelDefinition,
    LabelSchema,
    SourceResource,
)
from common.models import FileRegistry, Project

# Through the registry: annotations must not import a domain app (lint-imports).
Folder = apps.get_model("laparoscopy", "Folder")
Patient = apps.get_model("laparoscopy", "Patient")

_migration = importlib.import_module("annotations.migrations.0010_purge_video_annotations")


class PurgeVideoAnnotationsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        suffix = uuid.uuid4().hex[:8]
        cls.project = Project.objects.create(
            name=f"purge-{suffix}", slug=f"purge-{suffix}", domain="laparoscopy"
        )
        folder = Folder.objects.create(name="F", project=cls.project)
        cls.patient = Patient.objects.create(name="P", folder=folder, project=cls.project)
        video = FileRegistry.objects.create(
            domain="laparoscopy",
            file_type="video_raw",
            laparoscopy_patient=cls.patient,
            file_path="laparoscopy/raw/case.mp4",
            file_size=1,
            file_hash="a" * 64,
        )
        cls.resource = SourceResource.objects.create(
            kind=ResourceKind.FILE, identity_key=f"file:{video.pk}", file=video
        )
        cls.schema = LabelSchema.objects.create(
            name="Regions",
            slug=f"{_migration.SCHEMA_SLUG_PREFIX}{cls.project.pk}",
            domain="laparoscopy",
        )
        label = LabelDefinition.objects.create(
            schema=cls.schema, value=1, code="l1", display_name="Liver"
        )

        cls.video_set = cls._set("video_regions", label_schema=cls.schema)
        target = AnnotationTarget.objects.create(
            annotation_set=cls.video_set,
            source_resource=cls.resource,
            role="video",
            primary_slot=1,
        )
        selector = AnnotationSelector.objects.create(
            target=target,
            kind=SelectorKind.FRAME,
            coordinate_system=CoordinateSystem.VIDEO_PIXEL,
            frame_index=0,
        )
        revision = AnnotationRevision.objects.create(
            annotation_set=cls.video_set, revision_number=1
        )
        Geometry2DItem.objects.create(
            revision=revision,
            target=target,
            selector=selector,
            label=label,
            geometry_type=Geometry2DType.POLYGON,
            coordinate_system=CoordinateSystem.VIDEO_PIXEL,
            points=[[0, 0], [10, 0], [10, 10]],
            closed=True,
        )

        # Work that is not video and must survive the purge.
        cls.kept_set = cls._set("study_notes")
        kept_revision = AnnotationRevision.objects.create(
            annotation_set=cls.kept_set, revision_number=1
        )
        cls.kept_item = EventAnnotationItem.objects.create(
            revision=kept_revision, value="Adhesions noted"
        )

    @classmethod
    def _set(cls, kind, **fields):
        annotation_set = AnnotationSet.objects.create(kind=kind, **fields)
        annotation_set.set_patient(cls.patient)
        annotation_set.save()
        return annotation_set

    def test_the_purge_removes_video_sets_with_items_on_their_targets(self):
        _migration.purge(apps, None)

        self.assertFalse(AnnotationSet.objects.filter(kind__in=_migration.KINDS).exists())
        self.assertFalse(AnnotationTarget.objects.filter(annotation_set=self.video_set).exists())
        self.assertFalse(Geometry2DItem.objects.exists())
        self.assertFalse(LabelSchema.objects.filter(pk=self.schema.pk).exists())
        self.assertFalse(SourceResource.objects.filter(pk=self.resource.pk).exists())

    def test_the_purge_leaves_other_sets_alone(self):
        _migration.purge(apps, None)

        self.assertTrue(AnnotationSet.objects.filter(pk=self.kept_set.pk).exists())
        self.assertEqual(self.kept_set.revisions.count(), 1)
        self.assertTrue(EventAnnotationItem.objects.filter(pk=self.kept_item.pk).exists())
