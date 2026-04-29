from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('common', '0023_rename_common_file_domain_22309d_idx_maxillo_fil_domain_760eb4_idx_and_more'),
        ('maxillo', '0015_export_sharing_fields'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='IntraoralToothSegmentation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('teeth', models.JSONField(blank=True, default=dict, help_text='Map FDI tooth code to polygon sets [[[x, y], ...], ...] in image coordinates.')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('image_file', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='intraoral_segmentations', to='common.fileregistry')),
                ('patient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='intraoral_segmentations', to='maxillo.patient')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='updated_intraoral_segmentations', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['patient_id', 'image_file_id'],
                'indexes': [models.Index(fields=['patient', 'updated_at'], name='maxillo_int_patient_d8b901_idx'), models.Index(fields=['image_file'], name='maxillo_int_image_f_d24f72_idx')],
                'constraints': [models.UniqueConstraint(fields=('patient', 'image_file'), name='uniq_maxillo_seg_patient_image')],
            },
        ),
    ]
