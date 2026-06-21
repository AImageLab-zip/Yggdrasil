from django.db import migrations


class Migration(migrations.Migration):
    """
    Safe deletion of Classification model.
    Bite-classification fields (sagittal/vertical/transverse/midline) don't apply to
    Brain's MRI (T1/T2/FLAIR/T1c) workflow and were never surfaced in any brain template.
    Uses SeparateDatabaseAndState to handle the migration in two phases.
    """
    dependencies = [
        ('brain', '0014_patient_folders_m2m'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(name='Classification'),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="DROP TABLE IF EXISTS brain_classification;",
                    reverse_sql=migrations.RunSQL.noop,
                ),
            ],
        ),
    ]
