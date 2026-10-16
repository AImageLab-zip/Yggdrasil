"""Register the ``assisted_segmentation`` annotation method and enable it on laparoscopy projects.

The Assist tool is gated on it (and on ``ASSIST_WORKER_URL``/``ASSIST_WORKER_TOKEN``); a fresh
project gets it from ``setup_laparoscopy_modalities``.
"""

from django.db import migrations

SLUG = "assisted_segmentation"


def forwards(apps, schema_editor):
    AnnotationMethod = apps.get_model("common", "AnnotationMethod")
    Project = apps.get_model("common", "Project")
    method, _created = AnnotationMethod.objects.get_or_create(
        slug=SLUG,
        defaults={
            "name": "Assisted Segmentation",
            "description": "Masks from clicked points, painted by a model on the GPU worker.",
            "icon": "fas fa-wand-magic-sparkles",
            "domain": "laparoscopy",
            "is_active": True,
        },
    )
    for project in Project.objects.filter(domain="laparoscopy"):
        project.annotation_methods.add(method)


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0062_userpreference_label_colors"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
