"""Data migration: annotation-method registry cleanup.

- mri_caption was a duplicate of the common voice_caption method (brain uses
  the same VoiceCaption model): removed from every project and deleted.
- Remaining methods get clearer display names/descriptions.
"""

from django.db import migrations

METHOD_UPDATES = {
    "ios_landmarks": {
        "name": "IOS Landmarks",
        "description": "Manual tooth landmark annotation on IOS meshes.",
    },
    "bite_classification": {
        "name": "Bite Classification",
        "description": "AI bite classification review and acceptance.",
    },
    "intraoral_segmentation": {
        "name": "Intraoral Segmentation",
        "description": "Tooth polygon segmentation on intraoral photographs.",
    },
    "classification": {
        "name": "Occlusion classification",
        "description": "Sagittal / vertical / transverse (cephalometric) classification.",
    },
    "voice_caption": {
        "name": "Voice Captions",
        "description": "Audio and text captions attached to a patient.",
    },
    "video_regions": {
        "name": "Video Region Annotation",
        "description": "Frame-accurate region/quadrant annotation on surgical video.",
    },
}


def forwards(apps, schema_editor):
    AnnotationMethod = apps.get_model("common", "AnnotationMethod")

    # 1. Remove the duplicate brain captioning method.
    for method in AnnotationMethod.objects.filter(slug="mri_caption"):
        method.projects.clear()
        method.delete()

    # 2. Refresh names/descriptions.
    for slug, fields in METHOD_UPDATES.items():
        AnnotationMethod.objects.filter(slug=slug).update(**fields)


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0044_project_disabled_steps"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
