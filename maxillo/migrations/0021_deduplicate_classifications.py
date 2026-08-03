from django.db import migrations, models
from django.db.models import Count


def deduplicate_classifications(apps, schema_editor):
    Classification = apps.get_model('maxillo', 'Classification')
    duplicate_groups = (
        Classification.objects.exclude(patient_id=None)
        .values('patient_id', 'classifier')
        .annotate(row_count=Count('id'))
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


class Migration(migrations.Migration):

    dependencies = [
        ('maxillo', '0020_merge_20260519_1335'),
    ]

    operations = [
        migrations.RunPython(deduplicate_classifications, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='classification',
            constraint=models.UniqueConstraint(
                fields=('patient', 'classifier'),
                name='uniq_maxillo_class_patient_classifier',
            ),
        ),
    ]
