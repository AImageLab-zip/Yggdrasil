from django.db import migrations, models


def migrate_folder_to_folders(apps, schema_editor):
    Patient = apps.get_model("brain", "Patient")
    for patient in Patient.objects.exclude(folder__isnull=True).iterator():
        patient.folders.add(patient.folder_id)


class Migration(migrations.Migration):

    dependencies = [
        ("brain", "0013_alter_userpreference_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="patient",
            name="folders",
            field=models.ManyToManyField(blank=True, related_name="patients", to="brain.folder"),
        ),
        migrations.RunPython(migrate_folder_to_folders, migrations.RunPython.noop),
        migrations.RemoveIndex(
            model_name="patient",
            name="brain_patie_folder__37cf7a_idx",
        ),
        migrations.RemoveIndex(
            model_name="patient",
            name="brain_patie_folder__1cd6c7_idx",
        ),
        migrations.RemoveField(
            model_name="patient",
            name="folder",
        ),
    ]
