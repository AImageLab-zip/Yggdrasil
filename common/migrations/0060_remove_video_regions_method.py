"""Drop the ``video_regions`` annotation method: nothing gates on it any more."""

from django.db import migrations


def forwards(apps, schema_editor):
    AnnotationMethod = apps.get_model("common", "AnnotationMethod")
    for method in AnnotationMethod.objects.filter(slug="video_regions"):
        method.projects.clear()
        method.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0059_fileregistry_cardiology_patient_and_more"),
        # The sets that PROTECT the method are deleted there.
        ("annotations", "0010_purge_video_annotations"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
