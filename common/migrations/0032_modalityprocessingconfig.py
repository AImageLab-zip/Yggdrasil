from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('common', '0031_systemcheck'),
    ]

    operations = [
        migrations.CreateModel(
            name='ModalityProcessingConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('requires_processing', models.BooleanField(default=True)),
                ('queue_name', models.CharField(blank=True, default='', max_length=100)),
                ('is_blocking', models.BooleanField(default=True)),
                ('is_enabled', models.BooleanField(default=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('depends_on', models.ManyToManyField(blank=True, related_name='dependent_configs', to='common.modality')),
                ('modality', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='processing_config', to='common.modality')),
            ],
            options={
                'ordering': ['modality__name'],
            },
        ),
    ]
