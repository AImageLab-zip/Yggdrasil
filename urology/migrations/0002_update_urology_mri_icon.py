from django.db import migrations


def update_urology_mri_icon(apps, schema_editor):
    Modality = apps.get_model("common", "Modality")
    Modality.objects.filter(slug="urology-mri").update(icon="fas fa-magnet")


def reverse_urology_mri_icon(apps, schema_editor):
    Modality = apps.get_model("common", "Modality")
    Modality.objects.filter(slug="urology-mri").update(icon="fas fa-file-waveform")


class Migration(migrations.Migration):

    dependencies = [
        ("urology", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(update_urology_mri_icon, reverse_urology_mri_icon),
    ]
