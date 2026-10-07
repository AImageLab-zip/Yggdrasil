"""Delete every stored laparoscopy video annotation.

The video annotator and its backing tables were removed so the surface can be rebuilt
from scratch. This clears the *rows* only: the labelmap archives the payloads pointed at
are ``annotation_mask`` ``FileRegistry`` rows plus objects in storage, and object-storage
work is a management command, never a migration -- run
``manage.py laparoscopy_purge_annotation_masks`` after this.

Irreversible on purpose: there is no annotator left to read what it would restore.
"""

from django.db import migrations

KINDS = ("video_regions", "video_quadrants")
SCHEMA_SLUG_PREFIX = "laparoscopy-regions-project-"
VIDEO_FILE_TYPES = ("video_raw", "video_processed")


def purge(apps, schema_editor):
    AnnotationSet = apps.get_model("annotations", "AnnotationSet")
    AnnotationTarget = apps.get_model("annotations", "AnnotationTarget")
    LabelSchema = apps.get_model("annotations", "LabelSchema")
    SourceResource = apps.get_model("annotations", "SourceResource")

    # Cascades to targets, selectors, revisions, payloads and every item table.
    AnnotationSet.objects.filter(kind__in=KINDS).delete()
    # PROTECTed by the sets just deleted, so only reachable now.
    LabelSchema.objects.filter(slug__startswith=SCHEMA_SLUG_PREFIX).delete()
    # A video handle nothing is drawn on any more.
    SourceResource.objects.filter(
        kind="file", file__file_type__in=VIDEO_FILE_TYPES
    ).exclude(
        pk__in=AnnotationTarget.objects.values("source_resource_id")
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("annotations", "0009_annotationset_cardiology_patient"),
    ]

    operations = [
        migrations.RunPython(purge, migrations.RunPython.noop),
    ]
