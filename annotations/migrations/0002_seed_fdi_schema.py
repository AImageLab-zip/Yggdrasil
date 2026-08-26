"""Seed the FDI permanent-dentition vocabulary.

Data only -- ``sqlmigrate`` prints no DDL for this migration. It exists as a
migration rather than a management command because the conversion of the
existing tooth segmentations cannot run without it, and a vocabulary that has to
be seeded by hand before a deploy is a vocabulary that will be missing on one
environment.

FDI codes are two digits: quadrant (1-4, clockwise from the patient's upper
right) then tooth (1-8, from the midline outward). The integer ``value`` is
assigned sequentially in that order and is what a labelmap's voxels will hold,
so it is fixed from here on -- ``UniqueConstraint(schema, value)`` is what makes
that a guarantee rather than an intention. A future change to the numbering is a
new schema *version*, never an edit to this one.

Only permanent dentition, matching the validation
``maxillo.views.patient_data._normalize_landmarks_payload`` already enforces on
every landmark save (quadrants 1-4, teeth 1-8). Deciduous codes would need their
own schema; inventing values for them here would freeze a numbering nobody has
reviewed.
"""

from django.db import migrations

SCHEMA_SLUG = "fdi-permanent"
SCHEMA_VERSION = 1

#: Quadrant number -> the human name of the arch half it covers.
_QUADRANTS = {
    1: "upper right",
    2: "upper left",
    3: "lower left",
    4: "lower right",
}

#: Tooth number within a quadrant -> its name, from the midline outward.
_POSITIONS = {
    1: "central incisor",
    2: "lateral incisor",
    3: "canine",
    4: "first premolar",
    5: "second premolar",
    6: "first molar",
    7: "second molar",
    8: "third molar",
}


def _definitions():
    value = 0
    for quadrant in sorted(_QUADRANTS):
        for position in sorted(_POSITIONS):
            value += 1
            yield {
                "value": value,
                "code": f"{quadrant}{position}",
                "display_name": f"{_QUADRANTS[quadrant].title()} {_POSITIONS[position]}",
                "order": value,
            }


def forwards(apps, schema_editor):
    LabelSchema = apps.get_model("annotations", "LabelSchema")
    LabelDefinition = apps.get_model("annotations", "LabelDefinition")

    schema, _ = LabelSchema.objects.get_or_create(
        slug=SCHEMA_SLUG,
        version=SCHEMA_VERSION,
        defaults={
            "name": "FDI permanent dentition",
            "domain": "maxillo",
            "description": (
                "Two-digit FDI notation for the 32 permanent teeth. Values are "
                "frozen: an integer in an existing labelmap must not change meaning."
            ),
        },
    )

    # Idempotent per definition rather than per schema: a partially-applied run
    # (interrupted deploy, replayed migration) has to be able to finish rather
    # than skip because the schema row already exists.
    for definition in _definitions():
        LabelDefinition.objects.get_or_create(
            schema=schema,
            value=definition["value"],
            defaults={
                "code": definition["code"],
                "display_name": definition["display_name"],
                "order": definition["order"],
            },
        )


def backwards(apps, schema_editor):
    """Remove the seeded rows only if nothing references them.

    ``LabelDefinition`` is PROTECTed by every item that carries a label, so a
    schema in use cannot be deleted -- the reverse fails loudly instead of
    orphaning annotations. That is the correct outcome: unseeding a vocabulary
    that labelmaps depend on is not a reversal, it is data loss.
    """
    LabelSchema = apps.get_model("annotations", "LabelSchema")
    LabelSchema.objects.filter(slug=SCHEMA_SLUG, version=SCHEMA_VERSION).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("annotations", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
