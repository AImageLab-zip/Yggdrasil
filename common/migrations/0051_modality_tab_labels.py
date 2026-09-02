"""Data migration: give the intraoral photograph and teleradiography tabs a label.

``Modality.label`` is what the patient viewer prints on the modality tab
(``templates/maxillo/patient_detail_content.html``), falling back to ``name``.
Two maxillo rows never got a usable one: ``intraoral-photo`` was seeded with its
own *slug* as the label, so the tab read "intraoral-photo", and
``teleradiography`` had no label at all, so the tab read the full name. Beside
"IOS", "OPT" and "RAW" both are out of place.

The seeder now sets "IOP" and "TR", but ``get_or_create`` applies its defaults
only when it creates the row, so an existing deployment -- including the 1.9
dump this release migrates -- keeps the old text until something rewrites it.
This is that something. Rows a deployment has deliberately relabelled are left
alone: only the two exact values the seeder produced are replaced.
"""

from django.db import migrations

# slug -> (label the seeder used to leave behind, label it sets now)
RELABEL = {
    "intraoral-photo": ("intraoral-photo", "IOP"),
    "teleradiography": ("", "TR"),
}


def forwards(apps, schema_editor):
    Modality = apps.get_model("common", "Modality")

    for slug, (stale, wanted) in RELABEL.items():
        Modality.objects.filter(slug=slug, label=stale).update(label=wanted)


def backwards(apps, schema_editor):
    Modality = apps.get_model("common", "Modality")

    for slug, (stale, wanted) in RELABEL.items():
        Modality.objects.filter(slug=slug, label=wanted).update(label=stale)


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0050_alter_annotationmethod_created_by_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
