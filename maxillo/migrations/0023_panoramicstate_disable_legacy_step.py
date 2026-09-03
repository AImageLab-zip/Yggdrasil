import django.db.models.deletion
import uuid

from django.conf import settings
from django.db import migrations, models


def disable_legacy_panoramic_step(apps, schema_editor):
    ProcessingStep = apps.get_model('common', 'ProcessingStep')
    ProcessingStep.objects.filter(slug='cbct_to_panoramic').update(is_enabled=False)


def enable_legacy_panoramic_step(apps, schema_editor):
    ProcessingStep = apps.get_model('common', 'ProcessingStep')
    ProcessingStep.objects.filter(slug='cbct_to_panoramic').update(is_enabled=True)


class Migration(migrations.Migration):
    dependencies = [
        ('common', '0041_retire_audio_transcription_jobs'),
        ('maxillo', '0022_folder_is_demo'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PanoramicState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source_file_key', models.CharField(max_length=60)),
                ('source_file_hash', models.CharField(max_length=64)),
                ('source_segmentation_key', models.CharField(blank=True, max_length=60)),
                ('source_segmentation_hash', models.CharField(blank=True, max_length=64)),
                ('axial_slice', models.PositiveIntegerField()),
                ('volume_shape', models.JSONField()),
                ('spline', models.JSONField()),
                ('geometry_source', models.CharField(choices=[('auto', 'Automatic'), ('custom_cp', 'Edited')], default='custom_cp', max_length=10)),
                ('default_mode', models.CharField(choices=[('mean', 'Mean'), ('raysum', 'Ray sum')], max_length=10)),
                ('algorithm_version', models.CharField(default='panorex-js-v1', max_length=32)),
                ('revision', models.PositiveIntegerField(default=1)),
                ('generation_uuid', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('request_hash', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('generated_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='generated_panoramic_states', to=settings.AUTH_USER_MODEL)),
                ('mean_file', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='browser_panoramic_mean_states', to='common.fileregistry')),
                ('patient', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='panoramic_state', to='maxillo.patient')),
                ('raysum_file', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='browser_panoramic_raysum_states', to='common.fileregistry')),
                ('source_file', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='browser_panoramic_sources', to='common.fileregistry')),
                ('source_job', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='browser_panoramic_states', to='common.job')),
                ('source_segmentation_file', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='browser_panoramic_segmentation_sources', to='common.fileregistry')),
            ],
            options={'ordering': ['-updated_at']},
        ),
        migrations.RunPython(disable_legacy_panoramic_step, enable_legacy_panoramic_step),
    ]
