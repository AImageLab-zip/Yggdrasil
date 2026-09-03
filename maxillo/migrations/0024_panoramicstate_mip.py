import django.db.models.deletion

from django.db import migrations, models
from django.utils import timezone


V1_ALGORITHM = 'panorex-js-v1'
STALE_V1_ALGORITHM = 'panorex-js-v1-stale'


def mark_v1_states_stale(apps, schema_editor):
    PanoramicState = apps.get_model('maxillo', 'PanoramicState')
    ProcessingStep = apps.get_model('common', 'ProcessingStep')
    Job = apps.get_model('common', 'Job')
    PanoramicState.objects.filter(default_mode='mean').update(default_mode='mip')
    PanoramicState.objects.filter(algorithm_version=V1_ALGORITHM).update(
        algorithm_version=STALE_V1_ALGORITHM
    )
    ProcessingStep.objects.filter(slug='cbct_to_panoramic').update(is_enabled=False)
    Job.objects.filter(
        modality_slug='cbct_to_panoramic',
        status__in=['pending', 'dependency', 'processing', 'retrying'],
    ).update(
        status='failed',
        completed_at=timezone.now(),
        error_logs='Retired: panoramic generation now runs in the browser.',
    )


def restore_v1_states(apps, schema_editor):
    PanoramicState = apps.get_model('maxillo', 'PanoramicState')
    PanoramicState.objects.filter(
        algorithm_version=STALE_V1_ALGORITHM,
        default_mode='mip',
    ).update(default_mode='mean')
    PanoramicState.objects.filter(algorithm_version=STALE_V1_ALGORITHM).update(
        algorithm_version=V1_ALGORITHM
    )


class Migration(migrations.Migration):
    dependencies = [
        ('maxillo', '0023_panoramicstate_disable_legacy_step'),
    ]

    operations = [
        migrations.RenameField(
            model_name='panoramicstate',
            old_name='mean_file',
            new_name='mip_file',
        ),
        migrations.AlterField(
            model_name='panoramicstate',
            name='mip_file',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='browser_panoramic_mip_states',
                to='common.fileregistry',
            ),
        ),
        migrations.AlterField(
            model_name='panoramicstate',
            name='default_mode',
            field=models.CharField(
                choices=[
                    ('mip', 'Maximum intensity projection'),
                    ('raysum', 'Ray sum'),
                ],
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name='panoramicstate',
            name='algorithm_version',
            field=models.CharField(default='panorex-js-v2-mip', max_length=32),
        ),
        migrations.RunPython(mark_v1_states_stale, restore_v1_states),
    ]
