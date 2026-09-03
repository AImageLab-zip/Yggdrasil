"""Adopt the unique-per-classifier constraint production already carries.

Production applied ``maxillo/0021_deduplicate_classifications`` out of band, from the
unmerged ``feat/seg2pano-panoramic-variants`` branch. That migration deduplicated
``Classification`` by ``(patient, classifier)`` and added
``uniq_maxillo_class_patient_classifier``. The constraint is therefore already present
in a restored production database, while a fresh database (CI, dev, a scratch stack)
has neither the dedup nor the constraint -- and release/3.0's model state declared
neither.

So the database half is written to converge from both starting points: dedup only
touches rows that are actually duplicated, and the constraint is added only if the
table does not already carry it. The state half is a plain ``AddConstraint``, because
Django's recorded model state must gain it exactly once either way. A bare
``AddConstraint`` would work on a fresh database and fail on a restored one with a
duplicate key name.
"""

from django.db import migrations, models

CONSTRAINT_NAME = 'uniq_maxillo_class_patient_classifier'

CONSTRAINT = models.UniqueConstraint(
    fields=('patient', 'classifier'),
    name=CONSTRAINT_NAME,
)


def deduplicate_classifications(apps, schema_editor):
    """Collapse each (patient, classifier) group to one row.

    Keeps what the pre-3.0 dedup kept, so a database that already ran it converges to
    the same rows: for ``manual``, the *earliest* row (the human's original entry, not
    a later accidental overwrite); for anything else, the *latest* (the freshest model
    output). Ties break on id, which is monotonic.
    """
    Classification = apps.get_model('maxillo', 'Classification')
    duplicate_groups = (
        Classification.objects.exclude(patient_id=None)
        .values('patient_id', 'classifier')
        .annotate(row_count=models.Count('id'))
        .filter(row_count__gt=1)
    )

    for group in duplicate_groups.iterator():
        rows = Classification.objects.filter(
            patient_id=group['patient_id'],
            classifier=group['classifier'],
        )
        if group['classifier'] == 'manual':
            keeper = rows.order_by('timestamp', 'id').first()
        else:
            keeper = rows.order_by('-timestamp', '-id').first()
        rows.exclude(id=keeper.id).delete()


def _has_constraint(schema_editor, table):
    # introspection rather than information_schema so this holds on any backend the
    # test settings might use, not just MySQL.
    with schema_editor.connection.cursor() as cursor:
        return CONSTRAINT_NAME in schema_editor.connection.introspection.get_constraints(
            cursor, table
        )


def add_constraint_if_absent(apps, schema_editor):
    model = apps.get_model('maxillo', 'Classification')
    if _has_constraint(schema_editor, model._meta.db_table):
        return
    schema_editor.add_constraint(model, CONSTRAINT)


def drop_constraint_if_present(apps, schema_editor):
    model = apps.get_model('maxillo', 'Classification')
    if not _has_constraint(schema_editor, model._meta.db_table):
        return
    schema_editor.remove_constraint(model, CONSTRAINT)


class Migration(migrations.Migration):

    dependencies = [
        ('maxillo', '0030_alter_classification_sagittal_left_and_more'),
    ]

    operations = [
        migrations.RunPython(
            deduplicate_classifications,
            migrations.RunPython.noop,
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddConstraint(
                    model_name='classification',
                    constraint=CONSTRAINT,
                ),
            ],
            database_operations=[
                migrations.RunPython(
                    add_constraint_if_absent,
                    drop_constraint_if_present,
                ),
            ],
        ),
    ]
