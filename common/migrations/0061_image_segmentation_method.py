"""Register the ``image_segmentation`` annotation method and enable it on laparoscopy projects.

Existing laparoscopy projects get it because the page they open has an Annotation Mode
button gated on it; a fresh project gets it from ``setup_laparoscopy_modalities``.
"""

from django.db import migrations

SLUG = "image_segmentation"


def forwards(apps, schema_editor):
    AnnotationMethod = apps.get_model("common", "AnnotationMethod")
    Project = apps.get_model("common", "Project")
    method, _created = AnnotationMethod.objects.get_or_create(
        slug=SLUG,
        defaults={
            "name": "Image Segmentation",
            "description": "Pixel-accurate label masks on still images and video frames.",
            "icon": "fas fa-fill-drip",
            "domain": "laparoscopy",
            "is_active": True,
        },
    )
    for project in Project.objects.filter(domain="laparoscopy"):
        project.annotation_methods.add(method)


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0060_remove_video_regions_method"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
